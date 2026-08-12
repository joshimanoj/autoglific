(() => {
  "use strict";

  let state = null;
  let sessions = [];
  let sessionsLoadGeneration = 0;
  let settings = null;
  let sessionsLoading = false;
  let lastError = null;
  let lastSuccess = null;
  let busy = false;
  let generating = false;
  let glificPublishing = false;
  let glificStatusPoller = null;
  let workspaceMode = "authoring";
  let processPanel = "publishing";
  let reviewPanel = "mermaid";
  let publishingPhase = null;
  let mermaidRenderToken = 0;
  let mermaidRenderError = null;
  let mermaidConfigured = false;
  let renderedPresentationSource = "";
  let renderedQuestionKey = "";
  let clarificationRevealKey = "";
  let clarificationRevealGeneration = 0;
  let validationAutoAnswerKey = "";
  let toastTimer = null;
  let toastGeneration = 0;
  let toastVisible = false;
  let confettiFlowId = "";
  const TOAST_DURATION_MS = 2600;

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? "" : value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
  const pretty = (value) => JSON.stringify(value, null, 2);
  const compactJson = (value) => JSON.stringify(value);

  const CAPABILITY_NAMES = {
    send_text_message: "Send a message",
    capture_user_input: "Ask for information",
    fixed_choice: "Offer choices",
    persist_contact_field: "Save an answer",
    end: "End a branch",
  };
  const FIELD_NAMES = {
    copy: "Message",
    prompt: "Question",
    input_type: "Answer type",
    save_as: "Answer name used inside the flow",
    title: "Choice question",
    options: "Choices",
    source_variable: "Saved answer",
    field_name: "Contact record destination",
    reason: "Completion name",
    capture_reference: "Answer to save",
  };

  function fieldName(question) {
    return String(question?.field_path || "").replace(/^config\./, "");
  }

  function isValidationQuestion(question) { return fieldName(question) === "validation"; }
  function questionKeyFor(sessionId, question) { return [sessionId, question?.id, question?.field_path, question?.answer_type].join(":"); }
  function questionControlKey(question) { return questionKeyFor(state?.session?.id, question); }
  function answerTypeLabel(value) {
    return { text: "Text", number: "Number", email: "Email", phone: "Phone number" }[String(value)] || String(value || "");
  }

  function capabilityName(capability) {
    return CAPABILITY_NAMES[capability] || "Flow step";
  }

  function labelForStableValue(value) {
    for (const node of state?.session?.nodes || []) {
      if (node.capability !== "fixed_choice") continue;
      const option = (node.config?.options || []).find((item) => item.value === value);
      if (option) return option.label;
    }
    return String(value || "");
  }

  function branchLabel(path) {
    const labels = (path || []).map(labelForStableValue).filter(Boolean);
    return labels.length ? labels.join(" → ") + " branch" : "main flow";
  }

  function currentBranch(question = state?.current_question) {
    if (question?.position_path?.length) return branchLabel(question.position_path);
    const proposal = state?.session?.active_proposal;
    const position = (state?.open_positions || []).find((item) => item.id === proposal?.incoming_position_id);
    return branchLabel(position?.branch_path || []);
  }

  function clarificationContext(question) {
    const title = String(state?.session?.title || "this flow").trim();
    const branch = currentBranch(question);
    return branch && branch !== "main flow" ? "Building: " + title + " · " + branch : "Building: " + title;
  }

  function queueClarificationReveal(payload) {
    const question = payload?.current_question;
    clarificationRevealKey = payload?.session?.state === "waiting_for_answer" && question && !isValidationQuestion(question)
      ? questionKeyFor(payload.session.id, question)
      : "";
    clarificationRevealGeneration += 1;
  }

  function friendlyQuestion(question) {
    const field = fieldName(question);
    if (field === "options") return { heading: "What choices should people see?", explanation: "Add the labels people should see." };
    if (question?.contextual && question.prompt) return { heading: question.prompt, explanation: "Answer this detail to complete the step. The builder checks it against the flow rules." };
    const questions = {
      copy: ["What message should people receive for this step?", "Write the exact welcome or response that should appear in WhatsApp."],
      prompt: ["What question should people see for this step?", "Write the exact prompt that asks for the information."],
      input_type: ["What kind of answer should people give?", "Choose the format that best fits the answer you expect."],
      title: ["What question should introduce the choices?", "This is the wording people will see before they choose."],
      options: ["What choices should people see?", "Add the labels people should see."],
      capture_reference: ["Which earlier answer should be saved here?", "Choose the answer this step should save in the contact record."],
      source_variable: ["Which earlier answer should be saved here?", "Choose the earlier answer that belongs in this contact record update."],
      field_name: ["Where should this answer be saved in the contact record?", "Choose the contact-record destination your team uses for this answer."],
      reason: ["How should this completion be named?", "Give this finished branch a short name for your team."],
      save_as: ["How should this answer be named inside the flow?", "This private name lets later steps use the answer. People will not see it."],
      branch_target: ["Which branch should this step belong to?", "Choose the matching open path before adding anything."],
    };
    const copy = questions[field] || (question?.question_class === "semantic" ? ["What single action should this step take?", "Choose the one action you want to add now."] : ["What detail should we use for this step?", "Give the exact detail you want to approve."]);
    return { heading: copy[0], explanation: copy[1] };
  }

  const ERROR_MESSAGES = {
    P4_SESSION_NOT_FOUND: ["That saved flow could not be found.", "Start a new flow and continue from the beginning."],
    P4_INSTRUCTION_REQUIRED: ["Add an instruction first.", "Describe one clear flow action, then send it."],
    P4_UNSUPPORTED_CAPABILITY: ["I could not identify that step.", "Try sending a message, asking for information, offering choices, saving an answer, or ending a branch."],
    P4_AMBIGUOUS_INSTRUCTION: ["That includes more than one action.", "Split it into separate segments and add the first one again."],
    P4_CONFIGURATION_INCOMPLETE: ["One detail is still missing.", "Complete the current clarification before continuing."],
    P4_OPTION_ANSWER_INVALID: ["Choose one of the available options.", "Select an answer, then continue."],
    P4_CHOICE_OPTIONS_ANSWER_INVALID: ["Each choice needs a clear label.", "Add at least two different choices, then continue."],
    P4_INVALID_JSON_ANSWER: ["The answer could not be saved.", "Try the current flow again or ask the workbench owner to inspect the safe server error."],
    P4_REVISION_CONFLICT: ["This flow changed in another tab.", "Open the latest saved version, then continue from the current question."],
    P4_NOT_READY_FOR_REVIEW: ["The flow is not ready to review yet.", "Finish every open branch and answer the remaining question."],
    P4_COMPILE_REQUIRES_FROZEN_SESSION: ["The approved flow needs to be frozen first.", "Return to Review flow and confirm it before running the pipeline."],
    P4_GLIFIC_CONFIGURATION_MISSING: ["Glific publishing is not configured.", "Ask the workbench owner to set GLIFIC_PRODUCTION_BASE_URL, GLIFIC_PHONE, and GLIFIC_PASSWORD on the server."],
    P4_GLIFIC_CONFIGURATION_INVALID: ["The Glific connection settings need attention.", "Ask the workbench owner to check the HTTPS tenant URL and server-side settings. No flow was reported as published."],
    P4_GLIFIC_AUTHENTICATION_FAILED: ["Glific authentication failed.", "Ask the workbench owner to check the server-side phone and password. No flow was reported as published."],
    P4_GLIFIC_API_UNAVAILABLE: ["Glific could not be reached.", "Check the HTTPS tenant URL or network connection, then try again. No flow was reported as published."],
    P4_GLIFIC_RESPONSE_INVALID: ["Glific returned an unexpected response.", "Ask the workbench owner to check the configured Glific API version, then try again."],
    P4_GLIFIC_IMPORT_FAILED: ["Glific rejected the flow import.", "Review the compiled flow and Glific response, then try again. No publish success was reported."],
    P4_GLIFIC_FLOW_NAME_COLLISION: ["A Glific flow with this name already exists.", "Rename the new flow before publishing."],
    P4_GLIFIC_FLOW_IDENTITY_FAILED: ["Glific did not return the imported flow identity.", "The import cannot be treated as confirmed. Check the Glific account and try again."],
    P4_GLIFIC_REVISION_SAVE_FAILED: ["Glific could not save the imported draft.", "The flow was not reported as published. Check the Glific flow-editor response and try again."],
    P4_GLIFIC_PUBLISH_FAILED: ["Glific did not confirm publication.", "The flow is not reported as published. Review the Glific response and try again."],
    P4_GLIFIC_PIPELINE_NOT_READY: ["The flow is not ready for Glific.", "Retry the pipeline until every stage passes, then try again."],
    P4_GLIFIC_ARTIFACT_NOT_AVAILABLE: ["The compiled Glific file is not available.", "Retry the pipeline before publication."],
    P4_GLIFIC_PUBLISH_IN_PROGRESS: ["A Glific publish is already in progress.", "Wait for the current request to finish before trying again."],
    P4_GLIFIC_LOCAL_STATE_CHANGED: ["The local flow changed during the Glific publish.", "Check Glific before retrying so you do not create a duplicate flow."],
    P4_SEMANTIC_CONFIGURATION_MISSING: ["Semantic setup is not available yet.", "Ask the workbench owner to configure the local semantic connection, then retry."],
    P4_SEMANTIC_PROVIDER_FAILURE: ["I could not understand that step right now.", "Check the local semantic connection, then try the same instruction again."],
  };

  function friendlyError(error) {
    const code = error?.code || "P4_WORKBENCH_OPERATION_FAILED";
    const known = ERROR_MESSAGES[code];
    return { code, message: known ? known[0] : "Something needs your attention.", recovery: known ? known[1] : "Try the current action again, or open Advanced details for the safe technical report.", technical: String(error?.message || "") };
  }

  function clearToastTimer() {
    if (toastTimer !== null) window.clearTimeout(toastTimer);
    toastTimer = null;
    toastGeneration += 1;
  }
  function armToastTimer() {
    clearToastTimer();
    const generation = toastGeneration;
    toastTimer = window.setTimeout(() => {
      if (generation !== toastGeneration) return;
      toastVisible = false;
      toastTimer = null;
      renderAlerts();
    }, TOAST_DURATION_MS);
  }
  function showToast(kind, value) {
    if (kind === "error") { lastError = value; lastSuccess = null; }
    else { lastSuccess = value; lastError = null; }
    toastVisible = Boolean(value);
    if (toastVisible) armToastTimer();
    else clearToastTimer();
    renderAlerts();
  }
  function dismissToast() {
    toastVisible = false;
    clearToastTimer();
    renderAlerts();
  }

  async function request(path, options = {}) {
    const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = payload.error || { code: "HTTP_" + response.status, message: response.statusText };
      const failure = new Error(detail.message || "Request failed.");
      failure.code = detail.code;
      failure.status = response.status;
      throw failure;
    }
    return payload;
  }

  function revision() { return state?.session?.revision; }
  function isReviewReady() { return state?.session?.state === "ready_for_review" || state?.session?.state === "frozen"; }
  function currentTrigger() {
    const keywords = state?.session?.flow_trigger_metadata?.keywords;
    return Array.isArray(keywords) && keywords.length ? String(keywords[0]?.value || "").trim() : "";
  }
  function compiledArtifact() {
    const pipeline = state?.pipeline;
    const stage = pipeline?.stages?.find((item) => item.name === "engine3_glific_artifact");
    return pipeline?.all_stages_passed && stage?.status === "passed" && stage?.json ? stage : null;
  }

  function apply(payload, success = null, revealClarification = false) {
    state = payload;
    lastError = null;
    lastSuccess = null;
    toastVisible = false;
    clearToastTimer();
    if (success) {
      lastSuccess = success;
      toastVisible = true;
      armToastTimer();
    }
    if (revealClarification) queueClarificationReveal(payload);
    else clarificationRevealKey = "";
    if (payload?.glific_publish || (payload?.pipeline && workspaceMode === "authoring")) {
      workspaceMode = "publishing";
      processPanel = "publishing";
    }
    if (payload?.session?.state !== "frozen") generating = false;
    render();
  }

  function fail(error) {
    showToast("error", friendlyError(error));
    busy = false;
    render();
  }

  function setBusy(value) { busy = value; render(); }

  function renderAlerts() {
    const alerts = $("alerts");
    if (!alerts) return;
    if (!toastVisible) { alerts.replaceChildren(); return; }
    if (lastError) alerts.innerHTML = "<div class=\"alert error\" role=\"alert\"><strong>" + esc(lastError.message) + "</strong><span>" + esc(lastError.recovery) + "</span><button type=\"button\" class=\"alert-close\" aria-label=\"Dismiss notification\">×</button></div>";
    else if (lastSuccess) alerts.innerHTML = "<div class=\"alert success\" role=\"status\"><span>" + esc(lastSuccess) + "</span><button type=\"button\" class=\"alert-close\" aria-label=\"Dismiss notification\">×</button></div>";
    else alerts.replaceChildren();
  }

  function sessionStatus(item) {
    if (item.published) return "Published";
    if (item.state === "ready_for_review") return "Ready to review";
    if (item.state === "frozen") return "Locked";
    if (item.state === "waiting_for_answer") return "Needs an answer";
    if (item.state === "blocked") return "Needs attention";
    return item.segment_count ? "Draft · " + item.segment_count + " segment" + (item.segment_count === 1 ? "" : "s") : "New draft";
  }

  function sessionOrderKey(item) {
    const timestamp = item?.updated_at || item?.created_at;
    if (timestamp) return "0:" + String(timestamp);
    const match = String(item?.id || "").match(/^flow-([a-z0-9]+)-/i);
    return "1:" + (match ? match[1] : String(item?.id || ""));
  }
  function orderSessions(items) {
    return [...items].sort((left, right) => sessionOrderKey(right).localeCompare(sessionOrderKey(left)) || String(right.id).localeCompare(String(left.id)));
  }

  function renderLibrary() {
    const list = $("flow-list");
    if (!list) return;
    if (sessionsLoading) list.innerHTML = "<div class=\"library-message\">Loading saved flows…</div>";
    else if (!sessions.length) list.innerHTML = "<div class=\"library-message\">No saved flows yet. Create your first flow to begin.</div>";
    else list.innerHTML = sessions.map((item) => {
      const active = state?.session?.id === item.id;
      const keyword = Array.isArray(item.keywords) && item.keywords.length ? " · " + item.keywords[0] : "";
      return "<button type=\"button\" class=\"flow-item" + (active ? " active" : "") + "\" data-session-id=\"" + esc(item.id) + "\"><strong>" + esc(item.title) + "</strong><span>" + esc(sessionStatus(item) + keyword) + "</span></button>";
    }).join("");
    const savedCount = sessions.length + " saved";
    $("flow-count").textContent = savedCount;
    $("flow-count-display").textContent = savedCount;
  }

  function renderShell() {
    $("app").dataset.view = workspaceMode;
    $("authoring-view").classList.toggle("hidden", workspaceMode !== "authoring");
    $("review-view").classList.toggle("hidden", workspaceMode !== "review");
    $("process-view").classList.toggle("hidden", workspaceMode !== "publishing");
    $("flow-title").textContent = state?.session?.title || "Start a new flow";
    $("save-flow").disabled = !state || busy;
    $("review-flow").disabled = !isReviewReady() || busy;
    $("review-flow").textContent = workspaceMode === "review" ? "Reviewing flow" : "Review flow";
  }

  function formatValue(value) {
    if (value == null) return "";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (Array.isArray(value)) return value.map((item) => item && item.label ? item.label : String(item)).join(", ");
    if (typeof value === "object") return compactJson(value);
    return String(value).replace(/\r?\n/g, " ↵ ");
  }
  function answerSummary(record) {
    const field = String(record.field_path || "").replace(/^config\./, "");
    if (field === "input_type") return "Answer type: " + answerTypeLabel(record.value);
    if (field === "copy") return formatValue(record.value);
    if (field === "prompt") return "Question: " + formatValue(record.value);
    if (field === "title") return formatValue(record.value);
    if (field === "options") return "Choices: " + formatValue(record.value);
    if (field === "save_as") return "Answer name: " + formatValue(record.value);
    if (field === "source_variable") return "Saved answer: " + formatValue(record.value);
    if (field === "field_name") return "Contact field: " + formatValue(record.value);
    if (field === "capture_reference") return "Answer selected: " + formatValue(record.value);
    if (field === "reason") return "Completion: " + formatValue(record.value);
    return (FIELD_NAMES[field] || "Answer") + ": " + formatValue(record.value);
  }
  function nodeDisplay(node) {
    const config = node.config || {};
    if (node.capability === "send_text_message") return "“" + esc(config.copy || "") + "”";
    if (node.capability === "capture_user_input") {
      const details = [
        config.prompt ? "Question: “" + esc(config.prompt) + "”" : "Question recorded",
        config.input_type ? "Answer type: " + esc(answerTypeLabel(config.input_type)) : "",
        config.save_as ? "Answer name: “" + esc(config.save_as) + "”" : "",
      ].filter(Boolean);
      return details.join(" · ");
    }
    if (node.capability === "fixed_choice") return esc(config.title || "") + "<br><span class=\"choice-confirmation-options\">Choices: " + (config.options || []).map((item) => esc(item.label || "")).join("&nbsp;·&nbsp;") + "</span>";
    if (node.capability === "persist_contact_field") {
      const capture = (state?.session?.nodes || []).find((item) => item.capability === "capture_user_input" && item.config?.save_as === config.source_variable);
      const selectedAnswer = capture?.config?.prompt || config.source_variable || "the captured answer";
      return "Saved “" + esc(selectedAnswer) + "” in contact field “" + esc(config.field_name || "") + "”.";
    }
    if (node.capability === "end") return "Completed branch: “" + esc(config.reason || "completed") + "”.";
    return esc(capabilityName(node.capability));
  }
  function nodeSummary(node) { return "<span class=\"summary-kind\">" + esc(capabilityName(node.capability)) + "</span><span class=\"summary-detail\">" + nodeDisplay(node) + "</span>"; }
  function chatMessage(kind, body, label = "") { return "<article class=\"turn " + kind + "\"" + (label ? " role=\"group\" aria-label=\"" + esc(label) + "\"" : "") + "><div class=\"bubble\">" + body + "</div></article>"; }

  function sourceStatementKey(value) { return String(value == null ? "" : value).trim(); }
  function conversationTurns() {
    if (!state) return [];
    const session = state.session;
    const turns = [];
    const bySource = new Map();
    const nodeTurns = new Map();
    const addTurn = (text, activeProposal = null) => {
      const source = sourceStatementKey(text);
      const key = source || (activeProposal ? "proposal:" + activeProposal.id : "node:" + turns.length);
      let turn = bySource.get(key);
      if (!turn) {
        turn = { key, text: source || "Untitled segment", nodes: [], records: [], active: false };
        bySource.set(key, turn);
        turns.push(turn);
      }
      if (activeProposal) {
        turn.active = true;
        turn.proposalId = activeProposal.id;
      }
      return turn;
    };
    (session.nodes || []).forEach((node) => {
      const turn = addTurn(node.source_statement);
      turn.nodes.push(node);
      nodeTurns.set(node.id, turn);
    });
    const active = session.active_proposal;
    const activeTurn = active ? addTurn(active.statement, active) : null;
    (session.answer_records || []).forEach((record) => {
      if (record.source === "approved_versioned_policy") return;
      const turn = record.node_id ? nodeTurns.get(record.node_id) : active && record.proposal_id === active.id ? activeTurn : null;
      if (turn) turn.records.push(record);
    });
    return turns;
  }
  function segmentEntries() {
    const turns = conversationTurns();
    if (!turns.length) return [];
    const activeTurn = turns.find((turn) => turn.active);
    return turns.map((turn, index) => ({
      text: turn.text,
      current: activeTurn ? turn === activeTurn : index === turns.length - 1,
      status: turn.active ? (state.current_question && !isValidationQuestion(state.current_question) ? "Needs one detail" : "Working") : "Complete",
    }));
  }
  function segmentsHtml() {
    const entries = segmentEntries();
    if (!entries.length) return "<div class=\"empty-state\"><strong>Your flow statements will appear here</strong></div>";
    return entries.map((entry, index) => "<article class=\"segment" + (entry.current ? " current" : "") + "\"><span class=\"segment-number\">" + (index + 1) + "</span><div><p>" + esc(entry.text) + "</p>" + (entry.current ? "<span class=\"segment-state\">" + esc(entry.status) + "</span>" : "") + "</div></article>").join("");
  }
  function renderSegments() { $("segment-list").innerHTML = segmentsHtml(); }

  function conversationHtml() {
    if (!state) return chatMessage("assistant", "I’ll help you build one segment at a time, ask for missing details, show the complete journey for review, and publish only after you approve it.", "AutoGlific") + chatMessage("assistant", "What should happen first?", "AutoGlific");
    const session = state.session;
    const turns = conversationTurns();
    const messages = [];
    if (!turns.length) messages.push(chatMessage("assistant", "What should happen first?", "AutoGlific"));
    turns.forEach((turn) => {
      messages.push(chatMessage("user", esc(turn.text), "You"));
      const records = turn.records.filter((record) => fieldName(record) !== "validation");
      if (records.length) messages.push(chatMessage("user", "<ul>" + records.map((record) => "<li>" + esc(answerSummary(record)) + "</li>").join("") + "</ul>", "You"));
      turn.nodes.forEach((node) => messages.push(chatMessage("assistant result", "<div class=\"step-confirmation\"><span class=\"step-detail\">" + nodeDisplay(node) + "</span><span class=\"recorded-inline\" role=\"img\" aria-label=\"Recorded\"><span class=\"step-check\" aria-hidden=\"true\">✓</span><span class=\"sr-only\">Recorded</span></span></div>", "AutoGlific")));
      if (turn.active && session.state === "waiting_for_answer" && state.current_question && !isValidationQuestion(state.current_question)) {
        const copy = friendlyQuestion(state.current_question);
        messages.push(chatMessage("assistant question current-question", "<strong>" + esc(copy.heading) + "</strong><p>" + esc(copy.explanation) + "</p>", "AutoGlific · clarification"));
      }
    });
    if (session.state === "ready_for_review" || session.state === "frozen") messages.push(chatMessage("assistant completion", "Flow complete. Review the flow before generating or publishing anything by clicking the Review flow button at the top.", "AutoGlific"));
    if (session.state === "blocked") messages.push(chatMessage("assistant question", esc(session.blocked_error?.message || "This flow needs attention before it can continue."), "AutoGlific"));
    if (busy && workspaceMode === "authoring") messages.push(chatMessage("assistant working", "<span class=\"working\"><span class=\"spinner\"></span>Working on this segment…</span>", "AutoGlific"));
    return messages.join("");
  }
  function renderConversation() {
    const thread = $("thread");
    thread.innerHTML = conversationHtml();
    if (busy || ["ready_for_review", "frozen"].includes(state?.session?.state)) window.setTimeout(() => { thread.scrollTop = thread.scrollHeight; }, 0);
  }

  function toSnake(value) { return String(value || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").replace(/_+/g, "_").toLowerCase().slice(0, 40); }
  function genericTextControl(field) {
    const long = ["copy", "prompt", "title"].includes(field);
    const examples = { copy: "e.g. Thanks for reaching out — we will call you shortly.", prompt: "e.g. What phone number should we call?", title: "e.g. How would you like us to help?", reason: "e.g. Callback requested", save_as: "e.g. phone_number", source_variable: "e.g. phone_number", field_name: "e.g. preferred_phone" };
    const hint = { save_as: "People will not see this name. It is used only inside the flow.", source_variable: "Use the answer name from an earlier question.", field_name: "Use the contact field name your team already uses.", reason: "This name helps your team recognize the completed path." }[field] || "";
    const input = long ? "<textarea id=\"question-answer\" class=\"question-answer-text\" rows=\"4\" placeholder=\"" + esc(examples[field] || "") + "\"></textarea>" : "<input id=\"question-answer\" class=\"question-answer-text\" type=\"text\" placeholder=\"" + esc(examples[field] || "") + "\" autocomplete=\"off\" />";
    return "<label for=\"question-answer\">" + esc(FIELD_NAMES[field] || "Answer") + "</label>" + input + (hint ? "<p class=\"control-help\">" + esc(hint) + "</p>" : "");
  }
  function renderInputTypeControl() {
    const options = [["text", "Text"], ["number", "Number"], ["email", "Email"], ["phone", "Phone number"]];
    return "<fieldset class=\"choice-fieldset\"><legend>Choose one</legend><div class=\"choice-cards\" role=\"radiogroup\">" + options.map((item) => "<button type=\"button\" class=\"choice-card\" data-choice-value=\"" + item[0] + "\" aria-pressed=\"false\"><strong>" + item[1] + "</strong></button>").join("") + "</div></fieldset>";
  }
  function renderBooleanControl() { return "<fieldset class=\"choice-fieldset\"><legend>Choose one</legend><div class=\"choice-cards\" role=\"radiogroup\"><button type=\"button\" class=\"choice-card\" data-choice-value=\"true\" aria-pressed=\"false\"><strong>Yes</strong></button><button type=\"button\" class=\"choice-card\" data-choice-value=\"false\" aria-pressed=\"false\"><strong>No</strong></button></div></fieldset>"; }
  function choiceControl(question, legend) { return "<fieldset class=\"choice-fieldset\"><legend>" + esc(legend) + "</legend><div class=\"choice-cards\" role=\"radiogroup\">" + (question.options || []).map((label) => "<button type=\"button\" class=\"choice-card\" data-choice-value=\"" + esc(label) + "\" aria-pressed=\"false\"><strong>" + esc(label) + "</strong></button>").join("") + "</div></fieldset>"; }
  function renderOptionEditor(question) {
    const labels = Array.isArray(question.choice_labels) && question.choice_labels.length >= 2 ? question.choice_labels.slice(0, 10) : ["", ""];
    return "<div class=\"option-editor\"><div class=\"option-editor-heading\"><label>Choices</label><span class=\"option-count\" id=\"option-count\">" + labels.length + " choices</span></div><div id=\"option-rows\" class=\"option-rows\"></div><button type=\"button\" id=\"add-option\" class=\"secondary-button\">+ Add choice</button></div>";
  }
  function attachChoiceCards() { document.querySelectorAll("[data-choice-value]").forEach((button) => button.addEventListener("click", () => document.querySelectorAll("[data-choice-value]").forEach((item) => item.setAttribute("aria-pressed", item === button ? "true" : "false")))); }
  function optionRow(index, label = "") {
    const row = document.createElement("div");
    row.className = "option-row";
    row.innerHTML = "<div class=\"option-number\">" + (index + 1) + "</div><label class=\"sr-only\" for=\"choice-label-" + index + "\">Choice " + (index + 1) + " label</label><input id=\"choice-label-" + index + "\" type=\"text\" class=\"option-label\" placeholder=\"Choice " + (index + 1) + "\" autocomplete=\"off\" value=\"" + esc(label) + "\" /><button type=\"button\" class=\"remove-option small-button\" aria-label=\"Remove choice " + (index + 1) + "\">×</button>";
    const labelInput = row.querySelector(".option-label");
    row.querySelector(".remove-option").addEventListener("click", () => { if (document.querySelectorAll(".option-row").length > 2) { row.remove(); renumberOptions(); } });
    return row;
  }
  function renumberOptions() { document.querySelectorAll(".option-row").forEach((row, index) => { row.querySelector(".option-number").textContent = String(index + 1); row.querySelector(".option-label").id = "choice-label-" + index; row.querySelector(".option-label").placeholder = "Choice " + (index + 1); row.querySelector(".option-label").previousElementSibling.htmlFor = "choice-label-" + index; row.querySelector(".option-label").previousElementSibling.textContent = "Choice " + (index + 1) + " label"; row.querySelector(".remove-option").setAttribute("aria-label", "Remove choice " + (index + 1)); }); if ($("option-count")) $("option-count").textContent = document.querySelectorAll(".option-row").length + " choices"; }
  function attachOptionEditor(question) { const rows = $("option-rows"); if (!rows) return; const labels = Array.isArray(question.choice_labels) && question.choice_labels.length >= 2 ? question.choice_labels.slice(0, 10) : ["", ""]; labels.forEach((label, index) => rows.append(optionRow(index, label))); $("add-option").addEventListener("click", () => { const count = document.querySelectorAll(".option-row").length; if (count < 10) { rows.appendChild(optionRow(count)); renumberOptions(); rows.lastElementChild.querySelector(".option-label").focus(); } }); }
  function renderAnswerControl(question) {
    const field = fieldName(question);
    const control = $("answer-control");
    if (field === "branch_target") { control.innerHTML = choiceControl(question, "Open branches"); attachChoiceCards(); return; }
    if (field === "capture_reference") { control.innerHTML = choiceControl(question, "Earlier answers"); attachChoiceCards(); return; }
    if (field === "options") { control.innerHTML = renderOptionEditor(question); attachOptionEditor(question); return; }
    if (question.answer_type === "boolean") { control.innerHTML = renderBooleanControl(); attachChoiceCards(); return; }
    if (field === "input_type" || (question.answer_type === "options" && question.options?.length)) { control.innerHTML = renderInputTypeControl(question); attachChoiceCards(); return; }
    control.innerHTML = genericTextControl(field);
  }
  function autoAnswerValidation(question) {
    const key = [state?.session?.id, state?.session?.revision, question?.id].join(":");
    if (validationAutoAnswerKey === key || busy || !state || state.session.state !== "waiting_for_answer" || !isValidationQuestion(state.current_question) || state.current_question.id !== question.id) return;
    validationAutoAnswerKey = key;
    const sessionId = state.session.id;
    const expectedRevision = state.session.revision;
    busy = true;
    render();
    void (async () => {
      try {
        const payload = await request("/api/sessions/" + encodeURIComponent(sessionId) + "/answer", { method: "POST", body: JSON.stringify({ revision: expectedRevision, question_id: question.id, value: {} }) });
        renderedQuestionKey = "";
        busy = false;
        apply(payload, null, true);
        void loadSessions();
      } catch (error) { fail(error); }
    })();
  }
  function renderQuestion() {
    const question = state?.current_question;
    const waiting = Boolean(question && state?.session?.state === "waiting_for_answer" && workspaceMode === "authoring");
    if (waiting && isValidationQuestion(question)) {
      $("answer-form").classList.add("hidden");
      $("question-prompt").replaceChildren();
      $("answer-control").replaceChildren();
      renderedQuestionKey = "";
      if (!busy) autoAnswerValidation(question);
      return;
    }
    $("answer-form").classList.toggle("hidden", !waiting);
    if (!waiting) { $("question-prompt").replaceChildren(); $("answer-control").replaceChildren(); renderedQuestionKey = ""; return; }
    // The full clarification lives once in chronological chat; the bottom
    // region contains only compact flow context, the answer control, and Continue.
    $("question-prompt").innerHTML = "<span class=\"clarification-context\">" + esc(clarificationContext(question)) + "</span>";
    const key = questionControlKey(question);
    if (renderedQuestionKey !== key || !$("answer-control").childElementCount) {
      renderAnswerControl(question);
      renderedQuestionKey = key;
    }
    const revealPending = clarificationRevealKey === key;
    $("answer-submit").disabled = busy || revealPending;
    $("answer-form").setAttribute("aria-busy", String(busy || revealPending));
    $("answer-control").querySelectorAll("button, input, textarea").forEach((control) => { control.disabled = busy || revealPending; });
  }
  function getSelectedChoice() { const selected = document.querySelector("[data-choice-value][aria-pressed=\"true\"]"); return selected ? selected.dataset.choiceValue : null; }
  function stableChoiceValues(labels) {
    const used = new Set();
    return labels.map((label, index) => {
      const base = toSnake(label) || "option_" + (index + 1);
      let value = base;
      let suffix = 2;
      while (used.has(value)) value = base + "_" + suffix++;
      used.add(value);
      return value;
    });
  }
  function readAnswer(question) {
    const field = fieldName(question);
    if (field === "branch_target" || field === "capture_reference") { const value = getSelectedChoice(); if (value === null) { const error = new Error("Choose an available option to continue."); error.code = "P4_ROUTING_CLARIFICATION_INVALID"; throw error; } return value; }
    if (field === "options") {
      const labels = Array.from(document.querySelectorAll(".option-row .option-label")).map((input) => input.value.trim());
      const duplicateLabels = new Set(labels.map((label) => label.toLocaleLowerCase())).size !== labels.length;
      if (labels.length < 2 || labels.some((label) => !label) || duplicateLabels) { const error = new Error("Add at least two choices with different labels."); error.code = "P4_CHOICE_OPTIONS_ANSWER_INVALID"; throw error; }
      const values = stableChoiceValues(labels);
      return labels.map((label, index) => ({ label, value: values[index] }));
    }
    if (question.answer_type === "boolean" || field === "input_type" || (question.answer_type === "options" && question.options?.length)) { const value = getSelectedChoice(); if (value === null) { const error = new Error("Choose an answer to continue."); error.code = "P4_OPTION_ANSWER_INVALID"; throw error; } return question.answer_type === "boolean" ? value === "true" : value; }
    const control = $("question-answer");
    const value = control ? control.value.trim() : "";
    if (!value) { const error = new Error("Add an answer before continuing."); error.code = "P4_CONFIGURATION_INCOMPLETE"; throw error; }
    return value;
  }
  function renderComposer() {
    const instructionVisible = Boolean(state && workspaceMode === "authoring" && state.session.state === "editing");
    const lockedVisible = Boolean(state && workspaceMode === "authoring" && ["ready_for_review", "frozen"].includes(state.session.state));
    const blockedVisible = Boolean(state && workspaceMode === "authoring" && state.session.state === "blocked");
    $("instruction-form").classList.toggle("hidden", !instructionVisible);
    $("instruction").disabled = !instructionVisible || busy;
    $("send-instruction").disabled = !instructionVisible || busy;
    $("composer-hint").textContent = !state ? "Create a named flow to begin" : state.session.state === "waiting_for_answer" ? "Your answer is used exactly as entered" : blockedVisible ? "Flow needs attention" : lockedVisible ? "Review is required before the next action" : "Please share your flow one branch/step at a time.";
    $("locked-composer").classList.toggle("hidden", !(lockedVisible || blockedVisible));
    if (lockedVisible) $("locked-composer").innerHTML = "<span aria-hidden=\"true\">🔒</span><span>Flow complete — ready for review</span>";
    else if (blockedVisible) $("locked-composer").innerHTML = "<span aria-hidden=\"true\">⚠</span><span>Flow needs attention. Review the message above, then choose this flow again or start a new one.</span>";
    renderQuestion();
  }

  function presentationMermaidSource() { return state?.live_presentation_mermaid || state?.checkpoint?.presentation_mermaid || ""; }
  function authoredMermaidSource() { return presentationMermaidSource(); }
  function technicalAuthoredMermaidSource() { return state?.live_authored_mermaid || state?.checkpoint?.authored_mermaid || ""; }
  function invalidateMermaidRender() { mermaidRenderToken += 1; renderedPresentationSource = ""; }
  function showMermaidRenderError(token, error, canvas) {
    if (token !== mermaidRenderToken) return;
    mermaidRenderError = { code: "P4_MERMAID_RENDER_FAILED", technical: error instanceof Error ? error.message : String(error || "Unknown Mermaid error") };
    canvas.innerHTML = "<div class=\"graph-render-error\" role=\"alert\"><strong>We couldn’t draw the flow graph.</strong><span>Your approved flow is safe. Try rendering it again.</span><button type=\"button\" class=\"secondary-button retry-mermaid\">Retry graph</button></div>";
    canvas.querySelector(".retry-mermaid").addEventListener("click", () => { renderedPresentationSource = ""; renderAuthoredMermaid(presentationMermaidSource(), canvas.id); });
  }
  async function renderAuthoredMermaid(source, canvasId = "review-graph") {
    const canvas = $(canvasId);
    if (!canvas) return;
    const sourceText = String(source || "");
    const renderKey = canvasId + "\n" + sourceText;
    if (renderedPresentationSource === renderKey && canvas.querySelector("svg")) return;
    const token = ++mermaidRenderToken;
    mermaidRenderError = null;
    canvas.setAttribute("aria-label", "Rendered authored flow graph");
    canvas.replaceChildren();
    if (!sourceText || sourceText.includes("No authored nodes yet")) { canvas.innerHTML = "<div class=\"graph-empty\"><strong>Your flow will appear here</strong><span>Complete the authored flow to see its connected presentation.</span></div>"; renderedPresentationSource = renderKey; return; }
    canvas.innerHTML = "<div class=\"graph-render-loading\" role=\"status\">Rendering the approved flow…</div>";
    const mermaidApi = window.mermaid;
    if (!mermaidApi || typeof mermaidApi.initialize !== "function" || typeof mermaidApi.render !== "function") { showMermaidRenderError(token, new Error("The local Mermaid renderer is unavailable."), canvas); return; }
    try {
      if (!mermaidConfigured) { mermaidApi.initialize({ startOnLoad: false, securityLevel: "strict", theme: "base", flowchart: { htmlLabels: false, useMaxWidth: false, curve: "basis" } }); mermaidConfigured = true; }
      const rendered = await mermaidApi.render("p4-presentation-mermaid-" + token, sourceText);
      if (token !== mermaidRenderToken) return;
      if (!rendered || typeof rendered.svg !== "string" || !rendered.svg.includes("<svg")) throw new Error("Mermaid returned no SVG output.");
      canvas.innerHTML = rendered.svg;
      const svg = canvas.querySelector("svg");
      if (!svg) throw new Error("Mermaid returned an invalid SVG output.");
      svg.classList.add("mermaid-svg");
      svg.setAttribute("role", "img");
      svg.setAttribute("aria-label", "Rendered authored flow graph");
      svg.setAttribute("preserveAspectRatio", "xMinYMin meet");
      renderedPresentationSource = renderKey;
      if (typeof rendered.bindFunctions === "function") rendered.bindFunctions(canvas);
    } catch (error) { showMermaidRenderError(token, error, canvas); }
  }
  function renderReview() {
    if (workspaceMode !== "review" || !state) return;
    $("review-title").textContent = "Review " + state.session.title;
    const trigger = currentTrigger();
    const nodes = state.session.nodes || [];
    const edges = state.session.edges || [];
    $("graph-meta").textContent = (trigger ? "Trigger: " + trigger + " · " : "") + nodes.length + " authored step" + (nodes.length === 1 ? "" : "s") + " · " + edges.length + " connection" + (edges.length === 1 ? "" : "s");
    $("modify-flow").disabled = busy;
    $("confirm-flow").disabled = busy || !isReviewReady();
    renderTabState("review-tabs", reviewPanel, "reviewPanel");
    $("review-conversation-body").innerHTML = archiveConversationHtml();
    if (reviewPanel === "mermaid") window.setTimeout(() => renderAuthoredMermaid(presentationMermaidSource(), "review-graph"), 0);
  }

  const PIPELINE_DEFINITIONS = [
    ["frozen_package", "Flow Confirmation", "Confirmed authoring package"],
    ["engine1_graph", "Graph Normalization", "Frozen package → normalized graph"],
    ["engine2_flow_spec", "Flow Specification", "Normalized graph → flow specification"],
    ["engine3_glific_artifact", "Glific Payload Preparation", "Flow specification → importable payload"],
  ];
  const GLIFIC_PHASE_COPY = {
    connecting: { detail: "Connecting to the configured Glific tenant." },
    importing: { detail: "Importing the final Glific JSON payload." },
    publishing: { detail: "Publishing and waiting for authoritative confirmation." },
    reconciling: { detail: "Reconciling the exact remote flow identity." },
    readback: { detail: "Verifying the authoritative Glific state." },
  };
  const GLIFIC_SUBSTEPS = [
    ["connecting", "Connect", "Connecting", "Connected"],
    ["reconciling", "Reconcile", "Reconciling", "Reconciled"],
    ["importing", "Import", "Importing", "Imported"],
    ["publishing", "Publish", "Publishing", "Published"],
    ["readback", "Verify", "Verifying", "Verified"],
  ];
  function glificPhaseCopy(phase) { return GLIFIC_PHASE_COPY[phase] || GLIFIC_PHASE_COPY.connecting; }
  function glificSubsteps(phase, complete = false) {
    const currentIndex = complete ? GLIFIC_SUBSTEPS.length : Math.max(0, GLIFIC_SUBSTEPS.findIndex(([key]) => key === phase));
    const steps = GLIFIC_SUBSTEPS.map(([key, idle, active, done], index) => {
      const isDone = complete || index < currentIndex;
      const isCurrent = !complete && index === currentIndex;
      const className = isDone ? "done" : isCurrent ? "current" : "";
      const label = isDone ? done : isCurrent ? active : idle;
      return "<span class=\"pipeline-substep " + className + "\" data-phase=\"" + key + "\">" + esc(label) + "</span>";
    });
    return "<div class=\"pipeline-substeps\" aria-label=\"Glific publishing steps\">" + steps.join("<span class=\"pipeline-substep-arrow\" aria-hidden=\"true\">→</span>") + "</div>";
  }
  function launchConfetti(flowId) {
    if (!flowId || confettiFlowId === flowId || window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches) return;
    confettiFlowId = flowId;
    const layer = document.createElement("div");
    layer.className = "confetti-layer";
    layer.setAttribute("aria-hidden", "true");
    const colors = ["#315bd6", "#14936a", "#d09522", "#e66b8b", "#7a6ee6"];
    for (let index = 0; index < 22; index += 1) {
      const piece = document.createElement("span");
      piece.className = "confetti-piece";
      piece.style.left = ((index * 37) % 100) + "%";
      piece.style.setProperty("--confetti-color", colors[index % colors.length]);
      piece.style.setProperty("--confetti-delay", (index % 6) * 35 + "ms");
      piece.style.setProperty("--confetti-drift", (((index * 19) % 120) - 60) + "px");
      layer.appendChild(piece);
    }
    document.body.appendChild(layer);
    window.setTimeout(() => layer.remove(), 1900);
  }
  function stageDetail(stage, name) {
    if (!stage || stage.status !== "passed") return "";
    if (name === "engine1_graph" && Array.isArray(stage.json?.nodes) && Array.isArray(stage.json?.edges)) return stage.json.nodes.length + " normalized nodes · " + stage.json.edges.length + " connections";
    if (name === "engine2_flow_spec" && Array.isArray(stage.json?.flow?.nodes)) return stage.json.flow.nodes.length + " runtime nodes · specification validated";
    if (name === "engine2_flow_spec") return "Specification validated";
    if (name === "engine3_glific_artifact") return "Final Glific JSON artifact ready";
    return "Confirmed authoring package";
  }
  function pipelineStageStatus(name, stage) {
    let status = stage?.status || "waiting";
    if (name === "frozen_package" && state?.session?.state === "frozen") status = "passed";
    if (!state?.pipeline && name === "frozen_package" && ["preparing", "freezing"].includes(publishingPhase)) status = "working";
    if (!state?.pipeline && name === "engine1_graph" && publishingPhase === "compiling") status = "working";
    return status;
  }
  function pipelineStatusLabel(status) {
    return status === "passed" ? "Done" : status === "working" ? "In progress" : status === "error" ? "Failed" : "Waiting";
  }
  function pipelineMark(sequence) {
    return "<span class=\"pipeline-mark\" aria-hidden=\"true\">" + sequence + "</span>";
  }
  function pipelineStatusMarkup(status) {
    if (status === "passed") return "<span class=\"pipeline-state pipeline-complete\"><span class=\"pipeline-complete-check\" aria-hidden=\"true\">✓</span><span class=\"sr-only\">Done</span></span>";
    if (status === "working") return "<span class=\"pipeline-state pipeline-active\"><span class=\"pipeline-spinner\" aria-hidden=\"true\"></span><span>In progress</span></span>";
    if (status === "error") return "<span class=\"pipeline-state pipeline-failed\"><span class=\"pipeline-failed-mark\" aria-hidden=\"true\">!</span><span>Failed</span></span>";
    return "<span class=\"pipeline-state\">Waiting</span>";
  }
  function pipelineRows() {
    const pipeline = state?.pipeline;
    const rows = PIPELINE_DEFINITIONS.map(([name, title, subtitle], index) => {
      const stage = pipeline?.stages?.find((item) => item.name === name);
      const status = pipelineStageStatus(name, stage);
      const className = status === "passed" ? "complete" : status === "error" ? "failed" : status === "working" ? "running" : "";
      let detail = stageDetail(stage, name);
      if (status === "working" && name === "frozen_package") detail = publishingPhase === "freezing" ? "Freezing the approved authoring package." : "Preparing the confirmed flow.";
      if (status === "working" && name === "engine1_graph") detail = "Normalizing the frozen graph and preparing the deterministic payload.";
      if (!detail) detail = status === "error" ? "Needs attention" : status === "working" ? "Working on this stage." : subtitle;
      return "<div class=\"pipeline-step " + className + "\">" + pipelineMark(index + 1) + "<div><strong>" + esc(title) + "</strong><small>" + esc(detail) + "</small></div>" + pipelineStatusMarkup(status) + "</div>";
    });
    const glificStatus = state?.glific_publish ? "passed" : glificPublishing ? "working" : lastError?.code?.startsWith("P4_GLIFIC_") ? "error" : "waiting";
    const glificClass = glificStatus === "passed" ? "complete" : glificStatus === "working" ? "running" : glificStatus === "error" ? "failed" : "";
    const glificPhase = state?.glific_publish_status || (glificPublishing ? "connecting" : null);
    const glificDetail = state?.glific_publish ? "Glific confirmed the active flow." : glificPublishing ? glificPhaseCopy(glificPhase).detail : glificStatus === "error" ? "No confirmed publication." : compiledArtifact() ? "Waiting for the confirmed Glific step." : "Waiting for a ready payload.";
    const glificSubstepMarkup = glificStatus === "working" ? glificSubsteps(glificPhase) : glificStatus === "passed" ? glificSubsteps("readback", true) : "";
    rows.push("<div class=\"pipeline-step " + glificClass + "\">" + pipelineMark(5) + "<div><strong>Import and Publish</strong><small>" + esc(glificDetail) + "</small></div>" + pipelineStatusMarkup(glificStatus) + glificSubstepMarkup + "</div>");
    return rows.join("");
  }
  function renderPublished() {
    const published = state?.glific_publish;
    $("published-card").classList.toggle("hidden", !published);
    if (!published) return;
    $("published-heading").textContent = published.flow_name + " is published";
    $("published-detail").textContent = "Glific confirmed the active flow. " + (published.flow_id ? "Glific ID: " + published.flow_id + " · " : "") + "UUID: " + published.flow_uuid;
    const trigger = currentTrigger();
    const whatsapp = $("open-whatsapp");
    if (trigger) { whatsapp.href = "https://api.whatsapp.com/send/?phone=919403509920&text=" + encodeURIComponent(trigger) + "&type=phone_number&app_absent=0"; whatsapp.classList.remove("hidden"); }
    else { whatsapp.removeAttribute("href"); whatsapp.classList.add("hidden"); }
  }
  function renderPublishing() {
    if (workspaceMode !== "publishing" || !state) return;
    const published = Boolean(state.glific_publish);
    const artifact = compiledArtifact();
    $("pipeline-steps").innerHTML = pipelineRows();
    const retry = $("pipeline-retry");
    const retryCompile = state.session.state === "frozen" && Boolean(state.pipeline) && !state.glific_publish && !glificPublishing && !state.pipeline.all_stages_passed;
    retry.classList.toggle("hidden", !retryCompile);
    retry.disabled = busy;
    retry.textContent = "Retry pipeline";
    const publicationRetry = $("publication-retry");
    const retryPublication = Boolean(lastError?.code?.startsWith("P4_GLIFIC_") && artifact && !published && !glificPublishing);
    publicationRetry.classList.toggle("hidden", !retryPublication);
    publicationRetry.disabled = busy || glificPublishing;
    renderPublished();
  }
  function renderTabState(navId, selected, dataKey) {
    const nav = $(navId);
    if (!nav) return;
    const buttons = Array.from(nav.querySelectorAll("[role=\"tab\"]"));
    buttons.forEach((button) => {
      const active = button.dataset[dataKey] === selected;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      const panel = $(button.getAttribute("aria-controls"));
      if (panel) panel.classList.toggle("hidden", !active);
    });
  }
  function moveTabFocus(event, navId, dataKey) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const buttons = Array.from($(navId).querySelectorAll("[role=\"tab\"]"));
    const current = buttons.indexOf(event.currentTarget);
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
    event.preventDefault();
    buttons[next].focus();
    buttons[next].click();
  }
  function archiveConversationHtml() { return "<div class=\"archive-layout\"><section class=\"archive-thread\">" + conversationHtml() + "</section><aside class=\"archive-segments\"><h2>Flow Logic</h2><div class=\"segment-list\">" + segmentsHtml() + "</div></aside></div>"; }
  function renderProcessPanel() {
    if (workspaceMode !== "publishing" || !state) return;
    $("process-view").dataset.openPanel = processPanel;
    renderTabState("process-tabs", processPanel, "panel");
    if (processPanel === "conversation") $("process-conversation-body").innerHTML = archiveConversationHtml();
    else if (processPanel === "mermaid") { renderedPresentationSource = ""; window.setTimeout(() => renderAuthoredMermaid(presentationMermaidSource(), "process-mermaid-graph"), 0); }
  }
  function renderAdvanced() {
    if (!state) { $("advanced-content").textContent = ""; return; }
    const safe = { session: { id: state.session.id, title: state.session.title, revision: state.session.revision, state: state.session.state }, pipeline: state.pipeline, glific_publish: state.glific_publish, glific_publish_status: state.glific_publish_status, mermaid_render_error: mermaidRenderError, last_error: lastError ? { code: lastError.code, technical: lastError.technical } : null };
    $("advanced-content").textContent = pretty(safe);
  }
  function render() {
    renderAlerts(); renderLibrary(); renderShell(); renderConversation(); renderSegments(); renderComposer(); renderReview(); renderProcessPanel(); renderPublishing(); renderAdvanced();
    const revealKey = clarificationRevealKey;
    const generation = clarificationRevealGeneration;
    if (!revealKey || revealKey !== questionControlKey(state?.current_question)) return;
    window.setTimeout(() => {
      if (generation !== clarificationRevealGeneration || clarificationRevealKey !== revealKey) return;
      const questionBubble = $("thread").querySelector(".current-question");
      if (questionBubble?.scrollIntoView) questionBubble.scrollIntoView({ block: "center", behavior: "auto" });
      clarificationRevealKey = "";
      renderQuestion();
    }, 0);
  }

  async function loadSessions() {
    const generation = ++sessionsLoadGeneration;
    sessionsLoading = true;
    renderLibrary();
    try { const payload = await request("/api/sessions"); if (generation === sessionsLoadGeneration) sessions = orderSessions(Array.isArray(payload.sessions) ? payload.sessions : []); }
    finally { if (generation === sessionsLoadGeneration) { sessionsLoading = false; renderLibrary(); } }
  }
  async function submitInstructionText(statement) {
    if (!state || state.session.state !== "editing" || busy || !statement.trim()) return;
    setBusy(true);
    try { const payload = await request("/api/sessions/" + encodeURIComponent(state.session.id) + "/propose", { method: "POST", body: JSON.stringify({ revision: revision(), statement: statement.trim() }) }); $("instruction").value = ""; busy = false; apply(payload, "Segment recorded. Continue with the detail below.", true); void loadSessions(); }
    catch (error) { fail(error); }
  }
  async function createFlow(event) {
    event.preventDefault();
    const titleInput = $("flow-name");
    const title = titleInput.value.trim();
    if (!title) { $("name-error").textContent = "Enter a flow name to continue."; titleInput.focus(); return; }
    const suffix = Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 7);
    setBusy(true);
    try { const payload = await request("/api/sessions", { method: "POST", body: JSON.stringify({ session_id: "flow-" + suffix, title, reset: true }) }); workspaceMode = "authoring"; processPanel = "publishing"; publishingPhase = null; $("name-dialog").close(); titleInput.value = ""; $("name-error").textContent = ""; busy = false; window.history.replaceState({}, "", "?session=" + encodeURIComponent(payload.session.id)); apply(payload, "Flow created. Describe the first segment."); void loadSessions(); window.setTimeout(() => $("instruction").focus(), 0); }
    catch (error) { fail(error); }
  }
  async function resumeSessionFromUrl() {
    const params = new URLSearchParams(window.location.search);
    if (!params.has("session")) { render(); return; }
    const sessionId = (params.get("session") || "").trim();
    if (!sessionId) { const error = new Error("Session does not exist."); error.code = "P4_SESSION_NOT_FOUND"; fail(error); return; }
    try { const payload = await request("/api/sessions/" + encodeURIComponent(sessionId)); workspaceMode = payload.glific_publish || payload.pipeline ? "publishing" : "authoring"; processPanel = "publishing"; validationAutoAnswerKey = ""; renderedQuestionKey = ""; apply(payload, null, true); }
    catch (error) { state = null; workspaceMode = "authoring"; processPanel = "publishing"; publishingPhase = null; fail(error); }
  }
  async function selectSession(sessionId) {
    if (busy || !sessionId) return;
    try { const payload = await request("/api/sessions/" + encodeURIComponent(sessionId)); workspaceMode = payload.glific_publish || payload.pipeline ? "publishing" : "authoring"; processPanel = "publishing"; window.history.replaceState({}, "", "?session=" + encodeURIComponent(sessionId)); validationAutoAnswerKey = ""; renderedQuestionKey = ""; apply(payload, null, true); }
    catch (error) { fail(error); }
  }
  async function submitInstruction(event) { event.preventDefault(); await submitInstructionText($("instruction").value); }
  async function submitAnswer(event) {
    event.preventDefault();
    const question = state?.current_question;
    if (!question || busy) return;
    let value;
    try { value = readAnswer(question); } catch (error) { fail(error); return; }
    setBusy(true);
    try { const payload = await request("/api/sessions/" + encodeURIComponent(state.session.id) + "/answer", { method: "POST", body: JSON.stringify({ revision: revision(), question_id: question.id, value }) }); renderedQuestionKey = ""; busy = false; apply(payload, "Answer saved. Continue with the next detail.", true); void loadSessions(); }
    catch (error) { fail(error); }
  }
  async function confirmFlow() {
    if (!state || busy || !isReviewReady()) return;
    workspaceMode = "publishing"; processPanel = "publishing"; publishingPhase = "preparing"; dismissToast(); setBusy(true);
    try {
      const prepared = await request("/api/sessions/" + encodeURIComponent(state.session.id) + "/prepare-confirmation", { method: "POST", body: JSON.stringify({ revision: revision() }) });
      publishingPhase = "freezing"; apply(prepared);
      const frozen = await request("/api/sessions/" + encodeURIComponent(state.session.id) + "/freeze", { method: "POST", body: JSON.stringify({ revision: prepared.session.revision, confirmed_hash: prepared.prepared_hash }) });
      publishingPhase = "compiling"; apply(frozen);
      const compiled = await request("/api/sessions/" + encodeURIComponent(state.session.id) + "/compile", { method: "POST", body: JSON.stringify({ revision: frozen.session.revision }) });
      publishingPhase = compiled.pipeline?.all_stages_passed ? null : "failed";
      apply(compiled, compiled.pipeline?.all_stages_passed ? "The final Glific payload is ready; Glific confirmation is next." : "The pipeline needs attention before publishing."); busy = false; render();
      if (compiled.pipeline?.all_stages_passed) await pushToGlific(true);
    } catch (error) { publishingPhase = "failed"; fail(error); }
  }
  async function compilePipeline() {
    if (!state || busy || state.session.state !== "frozen") return;
    workspaceMode = "publishing"; processPanel = "publishing"; publishingPhase = "compiling"; generating = true; setBusy(true);
    try { const payload = await request("/api/sessions/" + encodeURIComponent(state.session.id) + "/compile", { method: "POST", body: JSON.stringify({ revision: revision() }) }); generating = false; publishingPhase = payload.pipeline?.all_stages_passed ? null : "failed"; busy = false; apply(payload, payload.pipeline?.all_stages_passed ? "The final Glific payload is ready; Glific confirmation is next." : "The pipeline needs attention. Retry safely from this panel."); }
    catch (error) { generating = false; publishingPhase = "failed"; fail(error); }
  }
  function stopGlificStatusPolling() { if (glificStatusPoller !== null) { window.clearInterval(glificStatusPoller); glificStatusPoller = null; } }
  function startGlificStatusPolling() {
    stopGlificStatusPolling();
    glificStatusPoller = window.setInterval(async () => { if (!state || !glificPublishing) { stopGlificStatusPolling(); return; } try { state = await request("/api/sessions/" + encodeURIComponent(state.session.id)); render(); } catch (_error) { /* The POST response remains the source of truth. */ } }, 350);
  }
  async function pushToGlific() {
    if (!state || busy || glificPublishing || !compiledArtifact()) return;
    const wasPublished = Boolean(state.glific_publish);
    workspaceMode = "publishing"; processPanel = "publishing"; glificPublishing = true; publishingPhase = "publishing"; dismissToast(); setBusy(true); startGlificStatusPolling();
    try { const payload = await request("/api/sessions/" + encodeURIComponent(state.session.id) + "/publish", { method: "POST", body: JSON.stringify({ revision: revision() }) }); stopGlificStatusPolling(); glificPublishing = false; publishingPhase = null; busy = false; apply(payload, "Glific confirmed publication."); if (!wasPublished && payload.glific_publish) launchConfetti(payload.session.id); }
    catch (error) { stopGlificStatusPolling(); glificPublishing = false; publishingPhase = "failed"; fail(error); }
  }
  async function downloadArtifact(kind) {
    if (!state) return;
    try { const link = document.createElement("a"); link.href = "/api/sessions/" + encodeURIComponent(state.session.id) + "/download/" + kind; link.download = kind === "glific" ? "glific.json" : kind === "presentation-mermaid" ? "presentation.mmd" : kind + ".json"; link.hidden = true; document.body.appendChild(link); link.click(); window.setTimeout(() => link.remove(), 1000); showToast("success", kind === "glific" ? "Final Glific JSON downloaded." : "Mermaid graph downloaded."); }
    catch (error) { fail(error); }
  }
  async function copyMermaid() { try { await navigator.clipboard.writeText(technicalAuthoredMermaidSource()); showToast("success", "Mermaid source copied."); } catch (error) { fail(error); } }
  function openNameDialog() { if (!busy) { $("name-error").textContent = ""; $("name-dialog").showModal(); window.setTimeout(() => $("flow-name").focus(), 0); } }
  async function openSettings() {
    $("settings-dialog").showModal();
    $("settings-details").innerHTML = "<dt>Status</dt><dd>Loading safe connection details…</dd>";
    try { settings = await request("/api/settings"); renderSettings(); }
    catch (error) { $("settings-details").innerHTML = "<dt>Status</dt><dd>Settings could not be loaded. " + esc(friendlyError(error).recovery) + "</dd>"; }
  }
  function renderSettings() { if (settings) $("settings-details").innerHTML = "<dt>Glific URL</dt><dd>" + esc(settings.glific_url || "Not configured") + "</dd><dt>Mobile number</dt><dd>" + esc(settings.mobile_number || "Not configured") + "</dd><dt>Password</dt><dd>" + esc(settings.password || "Not configured") + "</dd>"; }
  function openReviewPanel(panel) { if (state && workspaceMode === "review" && ["conversation", "mermaid"].includes(panel)) { reviewPanel = panel; if (panel === "mermaid") invalidateMermaidRender(); render(); } }
  function openProcessPanel(panel) { if (state && workspaceMode === "publishing") { processPanel = panel; renderedPresentationSource = ""; render(); } }

  if (window.__AUTOGLIFIC_TEST__) {
    window.__AUTOGLIFIC_TEST_API__ = {
      dismissToast,
      friendlyError,
      launchConfetti,
      pipelineRows,
      readAnswer,
      apply,
      clarificationContext,
      render,
      renderAlerts,
      renderTabState,
      setState(payload) { state = payload; workspaceMode = "authoring"; processPanel = "publishing"; },
      setTestFlags({ busy: nextBusy = false, glificPublishing: nextPublishing = false } = {}) { busy = nextBusy; glificPublishing = nextPublishing; },
      showToast,
      stableChoiceValues,
      renderOptionEditor,
      conversationHtml,
      segmentEntries,
      archiveConversationHtml,
      renderLibrary,
      setSessions(items) { sessionsLoading = false; sessions = orderSessions(items || []); renderLibrary(); },
    };
  }

  $("flow-list").addEventListener("click", (event) => { const button = event.target.closest("[data-session-id]"); if (button) void selectSession(button.dataset.sessionId); });
  $("new-flow").addEventListener("click", openNameDialog);
  $("name-form").addEventListener("submit", createFlow);
  $("cancel-name").addEventListener("click", () => $("name-dialog").close());
  $("settings-button").addEventListener("click", () => void openSettings());
  $("close-settings").addEventListener("click", () => $("settings-dialog").close());
  $("alerts").addEventListener("click", (event) => { if (event.target.closest(".alert-close")) dismissToast(); });
  $("instruction-form").addEventListener("submit", submitInstruction);
  $("answer-form").addEventListener("submit", submitAnswer);
  $("save-flow").addEventListener("click", () => showToast("success", "This flow is saved in the local workbench."));
  $("review-flow").addEventListener("click", () => { if (isReviewReady() && !busy) { workspaceMode = "review"; invalidateMermaidRender(); render(); } });
  $("modify-flow").addEventListener("click", () => { workspaceMode = "authoring"; renderedPresentationSource = ""; render(); window.setTimeout(() => $("instruction").focus(), 0); });
  $("confirm-flow").addEventListener("click", () => void confirmFlow());
  $("review-tabs").addEventListener("click", (event) => { const button = event.target.closest("[data-review-panel]"); if (button) openReviewPanel(button.dataset.reviewPanel); });
  $("review-tabs").addEventListener("keydown", (event) => moveTabFocus(event, "review-tabs", "reviewPanel"));
  $("process-view").addEventListener("click", (event) => { const button = event.target.closest("[data-panel]"); if (button) openProcessPanel(button.dataset.panel); });
  $("process-tabs").addEventListener("keydown", (event) => moveTabFocus(event, "process-tabs", "panel"));
  $("pipeline-retry").addEventListener("click", () => void compilePipeline());
  $("publication-retry").addEventListener("click", () => void pushToGlific());
  $("download-json").addEventListener("click", () => void downloadArtifact("glific"));
  $("download-mermaid").addEventListener("click", () => void downloadArtifact("presentation-mermaid"));
  void loadSessions().catch((error) => fail(error));
  void resumeSessionFromUrl();
})();
