from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from product4.capabilities.forms import (
    apply_non_user_policies,
    configuration_questions,
    safe_contextual_prompt,
    validate_complete,
)
from product4.capabilities.registry import require_capability
from product4.contracts.questions import PendingQuestion, QuestionAnswer, QuestionClass
from product4.contracts.session import (
    AnswerRecord,
    AuthoringSession,
    DraftEdge,
    DraftNode,
    NodeProposal,
    OpenPosition,
    RevisionRecord,
    SegmentRouting,
    SegmentRoutingOption,
    SessionState,
)

from .graph_positions import positions_for_node
from .interpreter import RegistryInterpreter
from .semantic_clarification import apply_semantic_answer, semantic_questions
from .trigger_metadata import (
    commit_trigger_metadata,
    parse_trigger_answer,
    trigger_questions,
    validate_provider_trigger_intent,
)


def _hash_session(session: AuthoringSession) -> str:
    payload = session.model_dump(mode="json", exclude={"revisions"})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class SessionStore:
    """Atomic JSON persistence with compare-and-swap revisions."""

    def __init__(self, path: Path):
        self.path = path

    def save(self, session: AuthoringSession, expected_revision: int | None = None) -> None:
        if self.path.exists() and expected_revision is not None:
            current = AuthoringSession.model_validate_json(self.path.read_text())
            if current.revision != expected_revision:
                raise ValueError("P4_REVISION_CONFLICT")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".p4-session-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(session.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def load(self) -> AuthoringSession:
        return AuthoringSession.model_validate_json(self.path.read_text())


class AuthoringService:
    def __init__(self, interpreter: RegistryInterpreter, *, workbench_mode: bool = False):
        self.interpreter = interpreter
        self.workbench_mode = workbench_mode

    def start(
        self,
        session_id: str,
        title: str,
        *,
        original_brief: str | None = None,
        semantic_translation_hash: str | None = None,
    ) -> AuthoringSession:
        session = AuthoringSession(
            id=session_id,
            title=title,
            original_brief=original_brief,
            semantic_translation_hash=semantic_translation_hash,
        )
        return self._record(session, "start", parent=None)

    @staticmethod
    def _incoming_edge(session: AuthoringSession, node_id: str) -> DraftEdge | None:
        return next((edge for edge in session.edges if edge.target_id == node_id), None)

    @staticmethod
    def _choice_option_label(node: DraftNode, stable_value: str | None) -> str | None:
        if not stable_value:
            return None
        option = next(
            (
                item for item in node.config.get("options", [])
                if isinstance(item, dict) and item.get("value") == stable_value
            ),
            None,
        )
        return str(option["label"]) if option and option.get("label") else None

    @staticmethod
    def _choice_option_id(group_id: str, stable_value: str) -> str:
        digest = hashlib.sha256(f"{group_id}:{stable_value}".encode()).hexdigest()[:12].upper()
        return f"OPT-{digest}"

    @classmethod
    def _position_has_choice_option(
        cls,
        session: AuthoringSession,
        position: OpenPosition,
        group_id: str,
        stable_value: str,
    ) -> bool:
        if position.parent_node_id == group_id and position.exit_key == stable_value:
            return True
        node_id = position.parent_node_id
        while node_id:
            incoming = cls._incoming_edge(session, node_id)
            if incoming is None:
                return False
            if incoming.source_id == group_id and incoming.stable_value == stable_value:
                return True
            node_id = incoming.source_id
        return False

    @classmethod
    def _position_labels(
        cls,
        session: AuthoringSession,
        position: OpenPosition,
    ) -> tuple[str, ...]:
        labels: list[str] = []
        if position.parent_node_id:
            parent = next(
                (node for node in session.nodes if node.id == position.parent_node_id),
                None,
            )
            if parent and parent.capability == "fixed_choice":
                label = cls._choice_option_label(parent, position.exit_key)
                if label:
                    labels.append(label)
        node_id = position.parent_node_id
        while node_id:
            incoming = cls._incoming_edge(session, node_id)
            if incoming is None:
                break
            source = next(
                (node for node in session.nodes if node.id == incoming.source_id),
                None,
            )
            if source and source.capability == "fixed_choice":
                label = cls._choice_option_label(source, incoming.stable_value)
                if label:
                    labels.append(label)
            node_id = incoming.source_id
        return tuple(reversed(labels))

    @classmethod
    def _routing_catalog(cls, session: AuthoringSession) -> dict[str, dict[str, Any]]:
        catalog: dict[str, dict[str, Any]] = {}
        for position in session.open_positions:
            labels = cls._position_labels(session, position)
            catalog[position.id] = {
                "label": labels[-1] if labels else "Main flow",
                "labels": list(labels),
                "positions": [position],
                "choice_group_id": position.parent_node_id,
            }
        for group in session.nodes:
            if group.capability != "fixed_choice":
                continue
            for option in group.config.get("options", []):
                if not isinstance(option, dict):
                    continue
                stable_value = str(option.get("value") or "")
                label = str(option.get("label") or "")
                if not stable_value or not label:
                    continue
                option_id = cls._choice_option_id(group.id, stable_value)
                positions = [
                    position
                    for position in session.open_positions
                    if cls._position_has_choice_option(
                        session, position, group.id, stable_value
                    )
                ]
                catalog[option_id] = {
                    "label": label,
                    "labels": [label],
                    "positions": positions,
                    "choice_group_id": group.id,
                }
        return catalog

    @classmethod
    def _authoring_context(cls, session: AuthoringSession) -> dict[str, Any]:
        catalog = cls._routing_catalog(session)
        groups: list[dict[str, Any]] = []
        for node in session.nodes:
            if node.capability != "fixed_choice":
                continue
            options: list[dict[str, Any]] = []
            for option in node.config.get("options", []):
                if not isinstance(option, dict):
                    continue
                stable_value = str(option.get("value") or "")
                label = str(option.get("label") or "")
                option_id = cls._choice_option_id(node.id, stable_value)
                entry = catalog.get(option_id, {})
                options.append({
                    "option_id": option_id,
                    "label": label,
                    "open_branch_ids": [
                        position.id for position in entry.get("positions", [])
                    ],
                    "open": bool(entry.get("positions")),
                })
            groups.append({
                "choice_group_id": node.id,
                "title": node.config.get("title"),
                "options": options,
            })
        open_branches = []
        for position in session.open_positions:
            labels = cls._position_labels(session, position)
            option_ids = [
                identifier
                for identifier, entry in catalog.items()
                if position in entry.get("positions", [])
                and identifier.startswith("OPT-")
            ]
            open_branches.append({
                "branch_id": position.id,
                "labels": list(labels),
                "option_ids": option_ids,
                "parent_node_id": position.parent_node_id,
            })
        current = next(
            (
                branch for branch in open_branches
                if branch["branch_id"] == session.active_position_id
            ),
            None,
        )
        return {
            "revision": session.revision,
            "current_branch": current,
            "choice_groups": groups,
            "open_branches": open_branches,
            "completed_nodes": [
                {
                    "id": node.id,
                    "capability": node.capability,
                    "statement": node.source_statement,
                }
                for node in session.nodes[-12:]
            ],
            "captures": [
                {
                    "capture_id": cls._capture_reference_for_node(node.id),
                    "concept": str(node.config.get("prompt") or node.source_statement),
                    "branch_path": list(cls._node_branch_path(session, node.id)),
                }
                for node in session.nodes
                if node.capability == "capture_user_input"
            ],
            "recent_segment_history": [
                {
                    "statement": node.source_statement,
                    "capability": node.capability,
                }
                for node in session.nodes[-8:]
            ],
            "queued_segment": [
                {
                    "capability": proposal.capability,
                    "statement": proposal.statement,
                    "translation_node_id": proposal.translation_node_id,
                }
                for proposal in session.queued_proposals
            ],
        }

    @staticmethod
    def _capture_reference_for_node(node_id: str) -> str:
        digest = hashlib.sha256(
            f"product4-workbench-capture:{node_id}".encode()
        ).hexdigest()[:12].upper()
        return f"CAP-{digest}"

    @classmethod
    def _node_branch_path(cls, session: AuthoringSession, node_id: str) -> tuple[str, ...]:
        values: list[str] = []
        current = node_id
        while True:
            incoming = cls._incoming_edge(session, current)
            if incoming is None:
                return tuple(reversed(values))
            if incoming.stable_value:
                values.append(incoming.stable_value)
            current = incoming.source_id

    @staticmethod
    def _branch_reaches(capture_path: tuple[str, ...], target_path: tuple[str, ...]) -> bool:
        return target_path[:len(capture_path)] == capture_path

    @classmethod
    def _capture_candidates(
        cls,
        session: AuthoringSession,
        position: OpenPosition,
    ) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        for node in session.nodes:
            if node.capability != "capture_user_input" or not node.config.get("save_as"):
                continue
            capture_path = cls._node_branch_path(session, node.id)
            if not cls._branch_reaches(capture_path, tuple(position.branch_path)):
                continue
            reference = cls._capture_reference_for_node(node.id)
            prompt = str(node.config.get("prompt") or node.source_statement).strip()
            label = f'Answer from “{prompt}”'
            candidates.append((reference, label))
        return candidates

    @staticmethod
    def _generated_capture_name(
        session: AuthoringSession,
        proposal: NodeProposal,
    ) -> str:
        source = str(
            proposal.semantic_concept
            or proposal.source_excerpt
            or proposal.statement
            or "answer value"
        )
        source = re.split(r"\b(?:then|and then)\b", source, maxsplit=1, flags=re.IGNORECASE)[0]
        words = re.findall(r"[A-Za-z0-9]+", source.casefold())
        ignored = {
            "a", "an", "and", "answer", "ask", "capture", "collect", "for", "from",
            "get", "input", "of", "person", "participant", "provide", "their", "the",
            "this", "to", "user", "value",
        }
        meaningful = [word for word in words if word not in ignored]
        base = "_".join(meaningful)[:48].strip("_") or "answer_value"
        used = {
            str(node.config.get("save_as"))
            for node in session.nodes
            if node.capability == "capture_user_input" and node.config.get("save_as")
        }
        used.update(
            str(item.supplied_values.get("save_as"))
            for item in session.queued_proposals
            if item.capability == "capture_user_input" and item.supplied_values.get("save_as")
        )
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

    @classmethod
    def _capture_link_question(
        cls,
        session: AuthoringSession,
        proposal: NodeProposal,
        position: OpenPosition,
    ) -> tuple[NodeProposal, PendingQuestion]:
        candidates = cls._capture_candidates(session, position)
        if not candidates:
            raise ValueError("P4_CAPTURE_REFERENCE_UNAVAILABLE")
        labels = {reference: label for reference, label in candidates}
        fallback = "Which earlier answer should be saved here?"
        prompt = safe_contextual_prompt(
            proposal.capture_reference_question,
            fallback,
        )
        digest = hashlib.sha256(
            f"{proposal.id}:capture-reference:{','.join(labels)}".encode()
        ).hexdigest()[:12].upper()
        normalized = proposal.model_copy(update={"capture_reference_options": labels})
        return normalized, PendingQuestion(
            id=f"Q-SEM-{digest}",
            question_class=QuestionClass.SEMANTIC,
            node_proposal_id=proposal.id,
            field_path="capture_reference",
            prompt=prompt,
            answer_type="options",
            options=list(labels.values()),
            translation_node_id=proposal.translation_node_id,
            capability=proposal.capability,
            position_path=proposal.position_path,
            node_statement=proposal.statement,
            source_excerpt=proposal.source_excerpt,
            choice_labels=list(labels.values()),
            contextual=True,
        )

    @classmethod
    def _resolve_capture_reference(
        cls,
        session: AuthoringSession,
        proposal: NodeProposal,
        position: OpenPosition,
    ) -> str | None:
        reference = proposal.capture_reference
        if not reference:
            return None
        node_id = session.capture_reference_map.get(reference)
        if node_id is None:
            node_id = next(
                (
                    node.id for node in session.nodes
                    if node.capability == "capture_user_input"
                    and cls._capture_reference_for_node(node.id) == reference
                ),
                None,
            )
        if node_id is None:
            raise ValueError("P4_CAPTURE_REFERENCE_INVALID")
        capture = next((node for node in session.nodes if node.id == node_id), None)
        if capture is None or capture.capability != "capture_user_input":
            raise ValueError("P4_CAPTURE_REFERENCE_INVALID")
        if not cls._branch_reaches(
            cls._node_branch_path(session, capture.id),
            tuple(position.branch_path),
        ):
            raise ValueError("P4_CAPTURE_REFERENCE_CROSS_BRANCH")
        source_variable = capture.config.get("save_as")
        if not source_variable:
            raise ValueError("P4_CAPTURE_REFERENCE_SOURCE_MISSING")
        return str(source_variable)

    @staticmethod
    def _current_position(session: AuthoringSession) -> OpenPosition:
        if session.active_position_id:
            position = next(
                (
                    item for item in session.open_positions
                    if item.id == session.active_position_id
                ),
                None,
            )
            if position is not None:
                return position
        if len(session.open_positions) == 1:
            return session.open_positions[0]
        raise ValueError("P4_ROUTING_CURRENT_AMBIGUOUS")

    @classmethod
    def _resolve_routing(
        cls,
        session: AuthoringSession,
        routing: SegmentRouting,
    ) -> list[OpenPosition]:
        catalog = cls._routing_catalog(session)
        if routing.kind == "current_branch":
            if routing.scope != "single_branch":
                raise ValueError("P4_ROUTING_SCOPE_INVALID")
            return [cls._current_position(session)]
        if routing.kind == "existing_branch":
            if routing.scope != "single_branch":
                raise ValueError("P4_ROUTING_SCOPE_INVALID")
            if not routing.option_id or routing.option_id not in catalog:
                raise ValueError("P4_ROUTING_ID_INVALID")
            positions = catalog[routing.option_id]["positions"]
            if len(positions) != 1:
                raise ValueError("P4_ROUTING_SCOPE_AMBIGUOUS")
            if (
                routing.choice_group_id
                and catalog[routing.option_id].get("choice_group_id")
                != routing.choice_group_id
            ):
                raise ValueError("P4_ROUTING_SCOPE_INVALID")
            return list(positions)
        if routing.kind == "choice_group":
            if not routing.choice_group_id:
                raise ValueError("P4_ROUTING_ID_INVALID")
            group = next(
                (
                    node for node in session.nodes
                    if node.id == routing.choice_group_id
                    and node.capability == "fixed_choice"
                ),
                None,
            )
            if group is None:
                raise ValueError("P4_ROUTING_ID_INVALID")
            positions = [
                position
                for position in session.open_positions
                if any(
                    cls._position_has_choice_option(
                        session, position, group.id, str(option.get("value") or "")
                    )
                    for option in group.config.get("options", [])
                    if isinstance(option, dict)
                )
            ]
            if routing.scope != "descendant_leaves":
                raise ValueError("P4_ROUTING_SCOPE_INVALID")
            if not positions:
                raise ValueError("P4_ROUTING_SCOPE_EMPTY")
            return positions
        raise ValueError("P4_ROUTING_CLARIFICATION_REQUIRED")

    @classmethod
    def _routing_question(
        cls,
        session: AuthoringSession,
        proposal: NodeProposal,
    ) -> tuple[NodeProposal, Any]:
        routing = proposal.routing
        catalog = cls._routing_catalog(session)
        choices: list[SegmentRoutingOption] = []
        for option in routing.options:
            entry = catalog.get(option.id)
            if entry is None or not entry.get("positions"):
                raise ValueError("P4_ROUTING_ID_INVALID")
            choices.append(SegmentRoutingOption(id=option.id, label=entry["label"]))
        if not choices:
            choices = [
                SegmentRoutingOption(id=position.id, label=entry["label"])
                for position in session.open_positions
                for entry in [catalog[position.id]]
            ]
        if not choices:
            raise ValueError("P4_ROUTING_SCOPE_EMPTY")
        normalized = proposal.model_copy(update={
            "routing": routing.model_copy(update={"options": choices}),
        })
        digest = hashlib.sha256(
            f"{proposal.id}:routing:{','.join(option.id for option in choices)}".encode()
        ).hexdigest()[:12].upper()
        active_position = next(
            (
                position for position in session.open_positions
                if position.id == session.active_position_id
            ),
            None,
        )
        question = PendingQuestion(
            id=f"Q-SEM-{digest}",
            question_class=QuestionClass.SEMANTIC,
            node_proposal_id=proposal.id,
            field_path="branch_target",
            prompt=(
                routing.question
                or "Which open branch should this instruction apply to?"
            ),
            answer_type="options",
            options=[option.label for option in choices],
            translation_node_id=proposal.translation_node_id,
            capability=proposal.capability,
            position_path=tuple(active_position.branch_path) if active_position else (),
            node_statement=proposal.statement,
            source_excerpt=proposal.source_excerpt,
            choice_labels=[option.label for option in choices],
        )
        return normalized, question

    @classmethod
    def _apply_routing_answer(
        cls,
        session: AuthoringSession,
        proposal: NodeProposal,
        value: Any,
    ) -> tuple[NodeProposal, list[OpenPosition]]:
        options = proposal.routing.options
        selected = next(
            (
                option for option in options
                if str(value) == option.id or str(value) == option.label
            ),
            None,
        )
        if selected is None:
            raise ValueError("P4_ROUTING_CLARIFICATION_INVALID")
        routing = SegmentRouting(
            kind="existing_branch",
            scope="single_branch",
            option_id=selected.id,
            source_excerpt=proposal.routing.source_excerpt,
        )
        routed = proposal.model_copy(update={"routing": routing})
        return routed, cls._resolve_routing(session, routing)

    @classmethod
    def _assign_segment_targets(
        cls,
        session: AuthoringSession,
        proposal: NodeProposal,
        queued: list[NodeProposal],
    ) -> tuple[NodeProposal, list[NodeProposal]]:
        if proposal.routing.kind == "clarification":
            return proposal, queued
        targets = cls._resolve_routing(session, proposal.routing)
        base = [proposal, *queued]
        expanded: list[NodeProposal] = []
        for target_index, target in enumerate(targets):
            for node_index, item in enumerate(base):
                proposal_id = item.id
                if target_index or node_index:
                    digest = hashlib.sha256(
                        f"{item.id}:{target.id}:{target_index}:{node_index}".encode()
                    ).hexdigest()[:12].upper()
                    proposal_id = f"{item.id}-{digest}"
                expanded.append(item.model_copy(update={
                    "id": proposal_id,
                    "target_position_id": target.id,
                }))
        if not expanded:
            raise ValueError("P4_ROUTING_SCOPE_EMPTY")
        return expanded[0], expanded[1:]

    def propose(
        self,
        session: AuthoringSession,
        statement: str,
        *,
        translation_node_id: str | None = None,
        position_path: tuple[str, ...] | list[str] | None = None,
        node_statement: str | None = None,
        source_excerpt: str | None = None,
        choice_labels: tuple[str, ...] | list[str] | None = None,
    ) -> AuthoringSession:
        self._require_state(session, SessionState.EDITING)
        if session.queued_proposals:
            raise ValueError("P4_SEGMENT_PENDING")
        before = session.model_copy(deep=True)
        try:
            position = self._current_position(session)
            context = self._authoring_context(session)
            proposal = self.interpreter.interpret(
                statement,
                position,
                context=context,
            )
            validate_provider_trigger_intent(proposal.flow_trigger_intent, statement)
            queued = self.interpreter.drain_segment_proposals(statement, position)
            for queued_proposal in queued:
                validate_provider_trigger_intent(
                    queued_proposal.flow_trigger_intent,
                    statement,
                )
            proposal, queued = self._assign_segment_targets(session, proposal, queued)
            session.queued_proposals = queued
            if proposal.routing.kind == "clarification":
                proposal, routing_question = self._routing_question(session, proposal)
                session.active_proposal = proposal
                session.pending_questions = [routing_question]
                session.state = SessionState.WAITING_FOR_ANSWER
                return self._record(session, "prepare_routing_question", parent=before.revision)
            position = next(
                (
                    item for item in session.open_positions
                    if item.id == proposal.target_position_id
                ),
                None,
            )
            if position is None:
                raise ValueError("P4_ROUTING_TARGET_STALE")
            self._prepare_proposal(
                session,
                proposal,
                position,
                translation_node_id=translation_node_id,
                position_path=position_path,
                node_statement=node_statement,
                source_excerpt=source_excerpt,
                choice_labels=choice_labels,
            )
            if session.pending_questions:
                session.state = SessionState.WAITING_FOR_ANSWER
                return self._record(session, "prepare_questions", parent=before.revision)
            return self._commit(session, before.revision)
        except Exception:
            session.__dict__.update(before.__dict__)
            raise

    def _prepare_proposal(
        self,
        session: AuthoringSession,
        proposal: NodeProposal,
        position: OpenPosition,
        *,
        translation_node_id: str | None = None,
        position_path: tuple[str, ...] | list[str] | None = None,
        node_statement: str | None = None,
        source_excerpt: str | None = None,
        choice_labels: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        session.active_position_id = position.id
        values = dict(proposal.supplied_values)
        acquisition_sources = dict(proposal.acquisition_sources)
        resolved_position_path = tuple(position.branch_path if position_path is None else position_path)
        resolved_translation_node_id = (
            translation_node_id
            or (proposal.translation_node_id if proposal.translation_node_id != "unbound" else None)
            or proposal.id
        )
        resolved_labels = tuple(str(label) for label in (choice_labels or proposal.choice_labels or ()))
        if not resolved_labels and isinstance(values.get("options"), list):
            resolved_labels = tuple(
                str(item["label"])
                for item in values["options"]
                if isinstance(item, dict) and item.get("label")
            )
        proposal = proposal.model_copy(update={
            "incoming_position_id": position.id,
            "translation_node_id": resolved_translation_node_id,
            "position_path": resolved_position_path,
            "choice_labels": resolved_labels,
            "source_excerpt": source_excerpt or proposal.source_excerpt,
            "statement": node_statement or proposal.statement,
        })
        if self.workbench_mode and proposal.capability == "capture_user_input" and "save_as" not in values:
            values["save_as"] = self._generated_capture_name(session, proposal)
            acquisition_sources["save_as"] = "derived:semantic-capture-name"
        if self.workbench_mode and proposal.capability == "persist_contact_field":
            linked_source = self._resolve_capture_reference(session, proposal, position)
            if linked_source is not None:
                if "source_variable" in values and values["source_variable"] != linked_source:
                    raise ValueError("P4_CAPTURE_REFERENCE_SOURCE_MISMATCH")
                values["source_variable"] = linked_source
                acquisition_sources["source_variable"] = "derived:semantic-capture-link"
        values = apply_non_user_policies(proposal.capability, values)
        proposal = proposal.model_copy(update={
            "supplied_values": values,
            "acquisition_sources": acquisition_sources,
        })
        self.interpreter.activate_segment_node(proposal.translation_node_id)
        trigger = trigger_questions(session, proposal)
        semantic = semantic_questions(self.interpreter.client, proposal, position)
        capture_question = None
        if self.workbench_mode and proposal.capability == "persist_contact_field" and "source_variable" not in values:
            proposal, capture_question = self._capture_link_question(session, proposal, position)
        configuration = configuration_questions(
            proposal.id,
            proposal.capability,
            values,
            translation_node_id=proposal.translation_node_id,
            position_path=proposal.position_path,
            node_statement=proposal.statement,
            source_excerpt=proposal.source_excerpt,
            choice_labels=proposal.choice_labels,
            contextual_questions=proposal.contextual_questions,
            workbench_mode=self.workbench_mode,
        )
        session.active_proposal = proposal
        session.pending_questions = [
            *trigger,
            *semantic,
            *([capture_question] if capture_question is not None else []),
            *configuration,
        ]

    def _activate_next_segment(self, session: AuthoringSession) -> None:
        if not session.queued_proposals:
            return
        proposal = session.queued_proposals.pop(0)
        position = next(
            (
                item for item in session.open_positions
                if item.id == proposal.target_position_id
            ),
            None,
        )
        if position is None:
            raise ValueError("P4_ROUTING_TARGET_STALE")
        self._prepare_proposal(session, proposal, position)

    def answer(self, session: AuthoringSession, answer: QuestionAnswer) -> AuthoringSession:
        self._require_state(session, SessionState.WAITING_FOR_ANSWER)
        before = session.model_copy(deep=True)
        try:
            question = next((item for item in session.pending_questions if item.id == answer.question_id), None)
            if not question:
                raise ValueError("P4_UNKNOWN_QUESTION")
            proposal = session.active_proposal
            assert proposal is not None
            if question.field_path == "branch_target":
                routed, _ = self._apply_routing_answer(session, proposal, answer.value)
                queued = [
                    item.model_copy(update={"routing": routed.routing})
                    for item in session.queued_proposals
                ]
                routed, queued = self._assign_segment_targets(session, routed, queued)
                session.queued_proposals = queued
                target = next(
                    (
                        item for item in session.open_positions
                        if item.id == routed.target_position_id
                    ),
                    None,
                )
                if target is None:
                    raise ValueError("P4_ROUTING_TARGET_STALE")
                self._prepare_proposal(session, routed, target)
                session.answer_records = [
                    *session.answer_records,
                    self._answer_record(session, question, answer, routed),
                ]
                if session.pending_questions:
                    session.state = SessionState.WAITING_FOR_ANSWER
                    return self._record(session, "answer_routing_question", parent=before.revision)
                return self._commit(session, before.revision)
            if self.workbench_mode and question.field_path == "capture_reference":
                selected = next(
                    (
                        reference for reference, label in proposal.capture_reference_options.items()
                        if str(answer.value) in {reference, label}
                    ),
                    None,
                )
                if selected is None:
                    raise ValueError("P4_CAPTURE_REFERENCE_ANSWER_INVALID")
                position = next(
                    (
                        item for item in session.open_positions
                        if item.id == proposal.incoming_position_id
                    ),
                    None,
                )
                if position is None:
                    raise ValueError("P4_CAPTURE_REFERENCE_TARGET_STALE")
                proposal = proposal.model_copy(update={"capture_reference": selected})
                self._prepare_proposal(session, proposal, position)
                if session.pending_questions:
                    session.state = SessionState.WAITING_FOR_ANSWER
                    return self._record(session, "answer_capture_reference", parent=before.revision)
                return self._commit(session, before.revision)
            if question.field_path == "flow.trigger_keywords":
                values = parse_trigger_answer(answer.value)
                proposal = proposal.model_copy(update={
                    "flow_trigger_intent": None,
                    "flow_trigger_answer": values,
                })
                session.active_proposal = proposal
                session.pending_questions = [
                    item for item in session.pending_questions if item.id != question.id
                ]
                session.answer_records = [
                    *session.answer_records,
                    self._answer_record(session, question, answer, proposal),
                ]
                if session.pending_questions:
                    return self._record(session, "answer_trigger_question", parent=before.revision)
                return self._commit(session, before.revision)
            if question.question_class is QuestionClass.SEMANTIC:
                proposal = apply_semantic_answer(proposal, question.field_path, answer.value)
                definition = require_capability(proposal.capability)
                allowed = {field.path for field in definition.fields}
                values = {
                    key: value for key, value in proposal.supplied_values.items()
                    if key in allowed
                }
                values = apply_non_user_policies(proposal.capability, values)
                acquisition_sources = {
                    key: source for key, source in proposal.acquisition_sources.items()
                    if key in values
                }
                if question.field_path and question.field_path.startswith("config."):
                    acquisition_sources.pop(question.field_path.removeprefix("config."), None)
                proposal = proposal.model_copy(update={
                    "supplied_values": values,
                    "acquisition_sources": acquisition_sources,
                })
                remaining_semantic = [
                    item for item in session.pending_questions
                    if item.id != question.id and item.question_class is QuestionClass.SEMANTIC
                ]
                session.pending_questions = [
                    *remaining_semantic,
                    *configuration_questions(
                        proposal.id,
                        proposal.capability,
                        values,
                        translation_node_id=proposal.translation_node_id,
                        position_path=proposal.position_path,
                        node_statement=proposal.statement,
                        source_excerpt=proposal.source_excerpt,
                        choice_labels=proposal.choice_labels,
                        contextual_questions=proposal.contextual_questions,
                        workbench_mode=self.workbench_mode,
                    ),
                ]
            else:
                values = dict(proposal.supplied_values)
                if question.field_path and question.field_path.startswith("semantic."):
                    raise ValueError("P4_QUESTION_CLASS_ISOLATION")
                values[str(question.field_path)] = answer.value
                proposal = proposal.model_copy(update={"supplied_values": values})
            session.active_proposal = proposal
            session.answer_records = [
                *session.answer_records,
                self._answer_record(session, question, answer, proposal),
            ]
            if question.question_class is QuestionClass.CONFIGURATION:
                session.pending_questions = [item for item in session.pending_questions if item.id != question.id]
            if session.pending_questions:
                return self._record(session, "answer_question", parent=before.revision)
            return self._commit(session, before.revision)
        except Exception:
            session.__dict__.update(before.__dict__)
            raise

    @staticmethod
    def _answer_record(
        session: AuthoringSession,
        question: PendingQuestion,
        answer: QuestionAnswer,
        proposal: NodeProposal,
    ) -> AnswerRecord:
        answer_id = hashlib.sha256(
            f"{question.id}:{session.revision}:{answer.value!r}".encode()
        ).hexdigest()[:12].upper()
        return AnswerRecord(
            id=f"ANS-{answer_id}",
            question_id=question.id,
            question_class=question.question_class.value,
            proposal_id=proposal.id,
            capability=proposal.capability,
            field_path=str(question.field_path),
            prompt=question.prompt,
            value=answer.value,
            source=answer.decision_source,
            rationale=answer.rationale,
            answered_at=answer.answered_at,
            model_identity=answer.model_identity,
            prior_answer_context_hash=answer.prior_answer_context_hash,
            revision=session.revision + 1,
        )

    def _commit(self, session: AuthoringSession, parent_revision: int) -> AuthoringSession:
        proposal = session.active_proposal
        assert proposal is not None
        values = {key: value for key, value in proposal.supplied_values.items() if not key.startswith("semantic.")}
        validate_complete(proposal.capability, values)
        next_trigger_metadata = commit_trigger_metadata(session, proposal)
        position = next(item for item in session.open_positions if item.id == proposal.incoming_position_id)
        if position.claimed_by:
            raise ValueError("P4_POSITION_ALREADY_CLAIMED")
        node_id = f"N{len(session.nodes) + 1:03d}"
        node = DraftNode(
            id=node_id, capability=proposal.capability, config=values,
            source_statement=proposal.statement,
            source_excerpt=proposal.source_excerpt,
            incoming_position_id=position.id,
        )
        new_edges = list(session.edges)
        if position.parent_node_id:
            edge_id = f"E{len(new_edges) + 1:03d}"
            stable = position.exit_key if position.exit_key != "next" else None
            label = None
            if stable:
                parent = next(item for item in session.nodes if item.id == position.parent_node_id)
                option = next((item for item in parent.config.get("options", []) if item["value"] == stable), None)
                label = option["label"] if option else stable
            new_edges.append(DraftEdge(
                id=edge_id, source_id=position.parent_node_id, target_id=node_id,
                exit_key=position.exit_key, stable_value=stable, label=label,
            ))
        remaining = [item for item in session.open_positions if item.id != position.id]
        generated = positions_for_node(node, position)
        session.nodes = [*session.nodes, node]
        session.edges = new_edges
        session.flow_trigger_metadata = next_trigger_metadata
        session.open_positions = [*generated, *remaining]
        if self.workbench_mode and node.capability == "capture_user_input":
            capture_reference_map = {
                **session.capture_reference_map,
                self._capture_reference_for_node(node.id): node.id,
                proposal.id: node.id,
                proposal.translation_node_id: node.id,
            }
            session.capture_reference_map = capture_reference_map
        updated_queue: list[NodeProposal] = []
        for queued in session.queued_proposals:
            if queued.target_position_id == position.id:
                if len(generated) != 1:
                    raise ValueError("P4_SEGMENT_TARGET_SPLIT")
                queued = queued.model_copy(update={"target_position_id": generated[0].id})
            if proposal.routing.scope == "descendant_leaves":
                queued = queued.model_copy(update={
                    "supplied_values": {
                        **queued.supplied_values,
                        **proposal.supplied_values,
                    },
                    "acquisition_sources": {
                        **queued.acquisition_sources,
                        **proposal.acquisition_sources,
                    },
                    "acquisition_source_quotes": {
                        **queued.acquisition_source_quotes,
                        **proposal.acquisition_source_quotes,
                    },
                })
            updated_queue.append(queued)
        session.queued_proposals = updated_queue
        session.active_position_id = (
            generated[0].id
            if generated
            else (remaining[0].id if remaining else None)
        )
        session.active_proposal = None
        session.pending_questions = []
        bound_records = [
            record.model_copy(update={"node_id": node_id, "capability": node.capability})
            if record.proposal_id == proposal.id and record.node_id is None
            else record
            for record in session.answer_records
        ]
        for field_path, source in sorted(proposal.acquisition_sources.items()):
            # These are transient workbench bindings, not user decisions. The
            # frozen package already carries the resolved config; recording an
            # internal binding as an extra package decision would mix policy
            # provenance into an authored requirement and fail Engine 1's
            # unchanged authored-provenance contract.
            if self.workbench_mode and source.startswith("derived:"):
                continue
            digest = hashlib.sha256(
                f"{proposal.id}:{field_path}:{source}".encode()
            ).hexdigest()[:12].upper()
            bound_records.append(AnswerRecord(
                id=f"ANS-{digest}",
                question_id=f"Q-DER-{digest}",
                question_class="configuration",
                proposal_id=proposal.id,
                node_id=node_id,
                capability=node.capability,
                field_path=field_path,
                prompt=(
                    proposal.acquisition_source_quotes.get(field_path)
                    or proposal.source_excerpt or proposal.statement
                    if source == "confirmed_prose"
                    else "Derived from the current unpersisted capture context."
                ),
                value=values[field_path],
                source=(source if source == "confirmed_prose" else "approved_versioned_policy"),
                revision=session.revision + 1,
            ))
        session.answer_records = bound_records
        if session.queued_proposals:
            self._activate_next_segment(session)
            session.state = (
                SessionState.WAITING_FOR_ANSWER
                if session.pending_questions
                else SessionState.EDITING
            )
        else:
            session.state = SessionState.EDITING if session.open_positions else SessionState.READY_FOR_REVIEW
        recorded = self._record(session, f"commit:{node.capability}", parent=parent_revision)
        # A provider-supplied remainder may be complete without clarification.
        # Commit it in order before exposing the composer again.
        if session.active_proposal is not None and not session.pending_questions:
            return self._commit(session, recorded.revision)
        return recorded

    def edit_node(self, session: AuthoringSession, node_id: str, config: dict[str, Any]) -> AuthoringSession:
        if session.state is SessionState.FROZEN:
            session = session.model_copy(deep=True)
            session.state = SessionState.READY_FOR_REVIEW
            session.frozen_package = None
            session.frozen_hash = None
        if session.state is not SessionState.READY_FOR_REVIEW:
            raise ValueError("P4_EDIT_STATE_INVALID")
        nodes = list(session.nodes)
        index = next((i for i, item in enumerate(nodes) if item.id == node_id), None)
        if index is None:
            raise ValueError("P4_NODE_NOT_FOUND")
        validate_complete(nodes[index].capability, config)
        nodes[index] = nodes[index].model_copy(update={"config": deepcopy(config)})
        parent = session.revision
        session.nodes = nodes
        return self._record(session, f"edit:{node_id}", parent=parent)

    @staticmethod
    def _require_state(session: AuthoringSession, state: SessionState) -> None:
        if session.state is not state:
            raise ValueError(f"P4_INVALID_STATE_TRANSITION: expected {state.value}, got {session.state.value}")

    @staticmethod
    def _record(session: AuthoringSession, operation: str, parent: int | None) -> AuthoringSession:
        if parent is not None:
            session.revision += 1
        record = RevisionRecord(
            revision=session.revision, parent_revision=parent,
            operation=operation, canonical_hash=_hash_session(session),
        )
        session.revisions = [*session.revisions, record]
        return AuthoringSession.model_validate(session.model_dump(mode="json"))
