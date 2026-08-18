(() => {
  "use strict";

  let state = null;
  let sessions = [];
  let sessionsLoadGeneration = 0;
  let flowLoadGeneration = 0;
  let settings = null;
  let sessionsLoading = false;
  let flowLoading = false;
  let loadingSessionId = "";
  let lastSelectedSessionId = "";
  let loadingAction = "";
  let loadingStatus = "";
  let lastError = null;
  let busy = false;
  let generating = false;
  let glificPublishing = false;
  let glificStatusPoller = null;
  let workspaceMode = "landing";
  let processPanel = "publishing";
  let reviewPanel = "mermaid";
  let publishingPhase = null;
  let mermaidRenderToken = 0;
  let mermaidRenderError = null;
  let mermaidConfigured = false;
  let renderedPresentationSource = "";
  const mermaidSvgCache = new Map();
  let renderedQuestionKey = "";
  let clarificationRevealKey = "";
  let clarificationRevealGeneration = 0;
  let threadRevealPending = false;
  let validationAutoAnswerKey = "";
  let toastTimer = null;
  let toastGeneration = 0;
  let toastVisible = false;
  let confettiFlowId = "";
  let authenticated = false;
  let authVisible = false;
  let authMode = "login";
  let authUser = null;
  let csrfToken = "";
  let pendingAuthAction = "";
  let setupMode = false;
  let accountMenuOpen = false;
  let logoutInProgress = false;
  let logoutGeneration = 0;
  let wizardStep = 1;
  let setupShownForUser = "";
  const flowMemoryCache = new Map();
  const libraryMemoryCache = new Map();
  let cacheNamespaceKey = "public";
  const TOAST_DURATION_MS = 2600;

  const $ = (id) => document.getElementById(id);
  const hasAuthUi = () => Boolean($("auth-view"));
  if (!hasAuthUi()) authenticated = true;
  const esc = (value) => String(value == null ? "" : value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
  const compactJson = (value) => JSON.stringify(value);

  function currentCacheNamespace() {
    const next = authenticated ? "user:" + String(authUser?.id || authUser?.email || "unknown") : "public";
    if (next !== cacheNamespaceKey) {
      flowMemoryCache.clear();
      libraryMemoryCache.clear();
      cacheNamespaceKey = next;
    }
    return next;
  }
  function clearClientCaches() {
    flowMemoryCache.clear();
    libraryMemoryCache.clear();
    cacheNamespaceKey = authenticated ? "user:" + String(authUser?.id || authUser?.email || "unknown") : "public";
  }
  function flowCacheKey(sessionId) { return currentCacheNamespace() + ":flow:" + String(sessionId); }
  function cacheFlowPayload(payload) {
    const id = payload?.session?.id;
    if (id) flowMemoryCache.set(flowCacheKey(id), payload);
  }
  function flowPath(sessionId) {
    const prefix = authenticated ? "/api/sessions/" : "/api/public/sessions/";
    return prefix + encodeURIComponent(sessionId);
  }

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
    if (question?.contextual && question.prompt) return { heading: question.prompt, explanation: "" };
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
    P4_INVALID_JSON_ANSWER: ["The answer could not be saved.", "Try the current flow again. If it continues, share the AutoGlific reference with the owner."],
    P4_REVISION_CONFLICT: ["This flow changed in another tab.", "Open the latest saved version, then continue from the current question."],
    P4_NOT_READY_FOR_REVIEW: ["The flow is not ready to review yet.", "Finish every open branch and answer the remaining question."],
    P4_COMPILE_REQUIRES_FROZEN_SESSION: ["The approved flow needs to be frozen first.", "Return to Review flow and confirm it before running the pipeline."],
    P4_GLIFIC_CONFIGURATION_MISSING: ["Connect Glific before publishing.", "Open Settings, save the HTTPS tenant URL, mobile number, and password, then try Publish again."],
    P4_AUTH_REQUIRED: ["Sign in to continue.", "Create an account or sign in to keep your flows private."],
    P4_AUTH_INVALID_CREDENTIALS: ["Email or password is incorrect.", "Check your details and try again."],
    P4_AUTH_EMAIL_EXISTS: ["An account already exists.", "Sign in with that email or use a different address."],
    P4_AUTH_RATE_LIMITED: ["Too many sign-in attempts.", "Wait a few minutes, then try again."],
    P4_AUTH_EMAIL_INVALID: ["Enter a valid email address.", "Check the email format and try again."],
    P4_AUTH_DISPLAY_NAME_INVALID: ["Enter a valid full name.", "Use 2–100 characters without control characters."],
    P4_AUTH_PASSWORD_INVALID: ["Password must be 8–128 characters.", "Use a password between 8 and 128 characters and try again."],
    P4_SHARED_FLOW_READ_ONLY: ["This shared template is review-only.", "Choose New flow to create a private copy you can edit."],
    P4_CSRF_REQUIRED: ["This form has expired.", "Refresh the page and try again."],
    P4_CREDENTIAL_ENCRYPTION_KEY_MISSING: ["Secure credential storage is unavailable.", "Ask the deployment owner to configure the credential encryption key."],
    P4_SEMANTIC_CREDENTIAL_MISSING: ["Add an OpenAI API key first.", "Open Settings, save your key, then try authoring again."],
    P4_GLIFIC_CONFIGURATION_INVALID: ["The Glific connection settings need attention.", "Ask the AutoGlific owner to check the HTTPS tenant URL and server-side settings. No flow was reported as published."],
    P4_GLIFIC_AUTHENTICATION_FAILED: ["Glific authentication failed.", "Ask the AutoGlific owner to check the server-side phone and password. No flow was reported as published."],
    P4_GLIFIC_API_UNAVAILABLE: ["Glific could not be reached.", "Check the HTTPS tenant URL or network connection, then try again. No flow was reported as published."],
    P4_GLIFIC_RESPONSE_INVALID: ["Glific returned an unexpected response.", "Ask the AutoGlific owner to check the configured Glific API version, then try again."],
    P4_GLIFIC_IMPORT_FAILED: ["Glific rejected the flow import.", "Review the compiled flow and Glific response, then try again. No publish success was reported."],
    P4_GLIFIC_FLOW_NAME_COLLISION: ["A Glific flow with this name already exists.", "Rename the new flow before publishing."],
    P4_GLIFIC_FLOW_IDENTITY_FAILED: ["Glific did not return the imported flow identity.", "The import cannot be treated as confirmed. Check the Glific account and try again."],
    P4_GLIFIC_REVISION_SAVE_FAILED: ["Glific could not save the imported draft.", "The flow was not reported as published. Check the Glific flow-editor response and try again."],
    P4_GLIFIC_PUBLISH_FAILED: ["Glific did not confirm publication.", "The flow is not reported as published. Review the Glific response and try again."],
    P4_GLIFIC_PIPELINE_NOT_READY: ["The flow is not ready for Glific.", "Retry the pipeline until every stage passes, then try again."],
    P4_GLIFIC_ARTIFACT_NOT_AVAILABLE: ["The compiled Glific file is not available.", "Retry the pipeline before publication."],
    P4_GLIFIC_PUBLISH_IN_PROGRESS: ["A Glific publish is already in progress.", "Wait for the current request to finish before trying again."],
    P4_GLIFIC_LOCAL_STATE_CHANGED: ["The local flow changed during the Glific publish.", "Check Glific before retrying so you do not create a duplicate flow."],
    P4_SEMANTIC_CONFIGURATION_MISSING: ["AutoGlific semantic setup is unavailable.", "Ask the AutoGlific owner to configure semantic authoring on the server, then retry."],
    P4_SEMANTIC_AUTHENTICATION_FAILED: ["AutoGlific could not authenticate semantic authoring.", "Ask the AutoGlific owner to check the server-side semantic credentials, then retry."],
    P4_SEMANTIC_PROJECT_ACCESS_FAILED: ["AutoGlific semantic project access was denied.", "Ask the AutoGlific owner to check the configured project access, then retry."],
    P4_SEMANTIC_MODEL_UNAVAILABLE: ["AutoGlific’s semantic model is unavailable.", "Ask the AutoGlific owner to check the configured model, then retry."],
    P4_SEMANTIC_QUOTA_EXCEEDED: ["AutoGlific semantic usage is over its quota.", "Ask the AutoGlific owner to check the provider quota before retrying."],
    P4_SEMANTIC_RATE_LIMITED: ["AutoGlific semantic authoring is temporarily rate-limited.", "Wait a moment, then try the same instruction again."],
    P4_SEMANTIC_NETWORK_FAILURE: ["AutoGlific could not reach semantic authoring.", "Check the server connection, then try the same instruction again."],
    P4_SEMANTIC_PROVIDER_UNAVAILABLE: ["AutoGlific semantic authoring is temporarily unavailable.", "Wait a moment, then try the same instruction again."],
    P4_SEMANTIC_PROVIDER_FAILURE: ["AutoGlific could not complete semantic authoring.", "Try the same instruction again. If it continues, share the AutoGlific reference with the owner."],
    P4_SEMANTIC_PROVIDER_RESPONSE_INVALID: ["AutoGlific received an unreadable semantic response.", "Try the same instruction again. If it continues, share the AutoGlific reference with the owner."],
    P4_SEMANTIC_PROVIDER_RESPONSE_EMPTY: ["AutoGlific received an empty semantic response.", "Try the same instruction again. If it continues, share the AutoGlific reference with the owner."],
    P4_TRANSLATION_AMBIGUOUS: ["AutoGlific could not determine the intended branch.", "Choose one of the current open branches, then try the instruction again."],
    P4_TRANSLATION_TRIGGER_ONLY: ["AutoGlific needs an authored flow action for that trigger.", "Add the first message or another flow action, then try again."],
    P4_TRANSLATION_CHOICE_SOURCE_MISMATCH: ["AutoGlific could not validate the choice options.", "Try the same instruction again. If it continues, share the AutoGlific reference with the owner."],
    P4_TRANSLATION_SEGMENT_NON_LINEAR_MIDPOINT: ["AutoGlific created branches from that choice.", "Add the choice first, then build the remaining steps separately in each branch."],
  };

  function friendlyError(error) {
    const code = error?.code || "P4_WORKBENCH_OPERATION_FAILED";
    const branchOptions = Array.isArray(error?.available_branches)
      ? [...new Set(error.available_branches.filter((item) => typeof item === "string" && item.trim()).slice(0, 10))]
      : [];
    let known = ERROR_MESSAGES[code]
      || (code.startsWith("P4_TRANSLATION_")
        ? ["AutoGlific could not validate that instruction.", "Try the same instruction again. If it continues, share the AutoGlific reference with the owner."]
        : null);
    if (code === "P4_TRANSLATION_AMBIGUOUS" && branchOptions.length) {
      known = [
        "AutoGlific could not match that branch.",
        "Choose one of the current open branches: " + branchOptions.join(", ") + ".",
      ];
    }
    const requestId = error?.request_id || error?.requestId || "";
    const displayCode = code.replace(/^P4_WORKBENCH_/, "P4_AUTOGLIFIC_");
    return {
      code,
      message: known ? known[0] : "AutoGlific could not complete that action.",
      recovery: known ? known[1] : "Try the current action again. If it continues, share the AutoGlific reference with the owner.",
      reference: requestId ? displayCode + " · " + requestId : displayCode,
      technical: "",
    };
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
    if (kind !== "error") return;
    lastError = value;
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
    const { headers: optionHeaders = {}, ...rest } = options;
    const method = String(rest.method || "GET").toUpperCase();
    const headers = { "Content-Type": "application/json", ...optionHeaders };
    if (!headers["X-CSRF-Token"] && !headers["x-csrf-token"] && !["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) {
      headers["X-CSRF-Token"] = csrfToken;
    }
    const response = await fetch(path, { ...rest, credentials: "same-origin", headers });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = payload.error || { code: "HTTP_" + response.status, message: response.statusText };
      const failure = new Error(detail.message || "Request failed.");
      failure.code = detail.code;
      failure.status = response.status;
      failure.request_id = detail.request_id || response.headers?.get?.("X-AutoGlific-Request-ID") || "";
      failure.available_branches = Array.isArray(detail.available_branches) ? detail.available_branches : [];
      if (["P4_AUTH_REQUIRED", "P4_AUTH_INVALID_SESSION"].includes(failure.code)) clearClientCaches();
      throw failure;
    }
    if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
      libraryMemoryCache.delete(currentCacheNamespace());
    }
    return payload;
  }

  function renderAuth() {
    if (!hasAuthUi()) return;
    const title = $("auth-title");
    const description = $("auth-description");
    const nameField = $("auth-name-field");
    const nameInput = $("auth-display-name");
    const passwordInput = $("auth-password");
    const submit = $("auth-submit");
    const switchButton = $("auth-switch");
    const registering = authMode === "register";
    if (title) title.textContent = authMode === "register" ? "Create an account to start." : "Sign in to create your own flow.";
    if (description) description.textContent = authMode === "register" ? "Use a native email and password account. No external identity provider is required." : "Your saved flows and provider credentials stay scoped to your account.";
    if (nameField) nameField.classList.toggle("hidden", !registering);
    if (nameInput) {
      nameInput.disabled = !registering;
      nameInput.required = registering;
    }
    if (passwordInput) passwordInput.autocomplete = registering ? "new-password" : "current-password";
    if (submit) submit.textContent = authMode === "register" ? "Create account" : "Sign in";
    if (switchButton) switchButton.textContent = authMode === "register" ? "Already have an account? Sign in" : "Need an account? Register";
    renderAccountMenu();
  }

  function accountInitials(displayName) {
    const parts = String(displayName || "Account").trim().split(/\s+/).filter(Boolean);
    const letters = parts.length > 1 ? parts.slice(0, 2).map((part) => Array.from(part)[0]) : Array.from(parts[0] || "A").slice(0, 2);
    return letters.join("").toUpperCase();
  }

  function renderAccountMenu() {
    const trigger = $("account-menu-trigger");
    if (!trigger) return;
    const panel = $("account-menu-panel");
    const avatar = $("account-avatar");
    const name = $("account-trigger-name");
    const email = $("account-trigger-email");
    const identityName = $("account-menu-identity-name");
    const identityEmail = $("account-menu-identity-email");
    const signedIn = Boolean(authenticated);
    const displayName = signedIn ? String(authUser?.display_name || "Account") : "Sign in";
    const address = signedIn ? String(authUser?.email || "") : "";
    if (avatar) {
      avatar.textContent = signedIn ? accountInitials(displayName) : "";
      avatar.classList.toggle("hidden", !signedIn);
    }
    if (name) {
      name.textContent = displayName;
      name.title = signedIn ? displayName : "Sign in";
    }
    if (email) {
      email.textContent = address;
      email.title = address;
      email.classList.toggle("hidden", !signedIn);
    }
    if (identityName) {
      identityName.textContent = displayName;
      identityName.title = signedIn ? displayName : "Sign in";
    }
    if (identityEmail) {
      identityEmail.textContent = address;
      identityEmail.title = address;
    }
    trigger.classList.toggle("anonymous", !signedIn);
    trigger.setAttribute("aria-label", signedIn ? displayName + (address ? ", " + address : "") : "Sign in");
    trigger.setAttribute("aria-expanded", String(signedIn && accountMenuOpen));
    trigger.setAttribute("aria-busy", String(logoutInProgress));
    trigger.disabled = logoutInProgress;
    if (signedIn) trigger.setAttribute("aria-haspopup", "menu");
    else trigger.removeAttribute("aria-haspopup");
    const logoutButton = $("logout-menu");
    if (logoutButton) {
      logoutButton.disabled = logoutInProgress;
      logoutButton.setAttribute("aria-busy", String(logoutInProgress));
      logoutButton.textContent = logoutInProgress ? "Signing out…" : "Sign out";
    }
    if (panel) panel.classList.toggle("hidden", !signedIn || !accountMenuOpen);
    const logoutStatus = $("logout-status");
    if (logoutStatus) logoutStatus.textContent = logoutInProgress ? "Signing out…" : "";
    const logoutProgress = $("logout-progress");
    if (logoutProgress) logoutProgress.classList.toggle("hidden", !logoutInProgress);
  }

  function closeAccountMenu({ restoreFocus = false } = {}) {
    if (!accountMenuOpen) return;
    accountMenuOpen = false;
    renderAccountMenu();
    if (restoreFocus) window.setTimeout(() => $("account-menu-trigger")?.focus(), 0);
  }

  function toggleAccountMenu() {
    if (!authenticated) {
      showAuth("login");
      return;
    }
    accountMenuOpen = !accountMenuOpen;
    renderAccountMenu();
    if (accountMenuOpen) window.setTimeout(() => $("settings-menu")?.focus(), 0);
  }

  function moveAccountMenuFocus(event) {
    if (!accountMenuOpen) return;
    if (event.key === "Tab") {
      closeAccountMenu();
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const ids = ["settings-menu", "logout-menu"];
    const current = ids.indexOf(document.activeElement?.id);
    const next = event.key === "Home" ? 0 : event.key === "End" ? ids.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + ids.length) % ids.length;
    event.preventDefault();
    $(ids[next])?.focus();
  }

  function showAuth(mode = "login", action = "") {
    if (!hasAuthUi()) return;
    const preserveWorkspace = workspaceMode !== "landing" && action !== "resume" && action !== "new-flow";
    closeAccountMenu();
    authMode = mode === "register" ? "register" : "login";
    pendingAuthAction = action || pendingAuthAction;
    authVisible = true;
    if (!preserveWorkspace) {
      sessions = [];
      sessionsLoading = false;
      sessionsLoadGeneration += 1;
      settings = null;
      clearSettingsUi();
      state = null;
      workspaceMode = "landing";
    }
    renderAuth();
    render();
    window.setTimeout(() => $("auth-email")?.focus(), 0);
  }

  async function bootstrapAuth() {
    if (!hasAuthUi()) return;
    try {
      const csrf = await request("/api/auth/csrf");
      csrfToken = String(csrf.csrf_token || "");
      const current = await request("/api/auth/me");
      authenticated = current.authenticated === true;
      authUser = current.user || null;
      currentCacheNamespace();
      authVisible = false;
      render();
    } catch (_error) {
      authenticated = false;
      authUser = null;
      clearClientCaches();
      authVisible = false;
      render();
    }
  }

  async function submitAuth(event) {
    event.preventDefault();
    if (!hasAuthUi() || busy) return;
    const email = $("auth-email")?.value.trim() || "";
    const password = $("auth-password")?.value || "";
    const displayName = $("auth-display-name")?.value || "";
    const errorElement = $("auth-error");
    if (errorElement) errorElement.textContent = "";
    busy = true;
    render();
    try {
      const body = { email, password };
      if (authMode === "register") body.display_name = displayName;
      const payload = await request("/api/auth/" + authMode, { method: "POST", body: JSON.stringify(body) });
      authenticated = payload.authenticated === true;
      authUser = payload.user || null;
      currentCacheNamespace();
      csrfToken = String(payload.csrf_token || csrfToken);
      if ($("auth-password")) $("auth-password").value = "";
      busy = false;
      authVisible = false;
      if (workspaceMode === "landing") workspaceMode = "authoring";
      render();
      const userKey = String(authUser?.id || authUser?.email || "account");
      if (setupShownForUser !== userKey) {
        setupShownForUser = userKey;
        if (pendingAuthAction === "settings") pendingAuthAction = "";
        await openSettings({ setup: true });
        // The setup dialog owns continuation.  Its Done/Skip action resumes
        // a pending new-flow or saved-flow request after the user decides.
        return;
      }
      resumePendingAuthAction();
    } catch (error) {
      busy = false;
      if (errorElement) errorElement.textContent = friendlyError(error).message;
      render();
    }
  }

  function resumePendingAuthAction() {
    const action = pendingAuthAction;
    pendingAuthAction = "";
    if (action === "new-flow" && authenticated) {
      if (settings?.authoring_configured !== true) {
        workspaceMode = "authoring";
        render();
        void loadSessions().catch((error) => fail(error));
      } else startNewFlow();
    }
    else if (action === "resume" && authenticated) void resumeSessionFromUrl();
    else if (action === "settings" && authenticated) void openSettings();
    else if (!action && authenticated) void resumeAuthenticatedWorkspace();
  }

  async function resumeAuthenticatedWorkspace() {
    if (!authenticated) return;
    workspaceMode = "authoring";
    render();
    const requestedSession = new URLSearchParams(window.location.search).get("session");
    if (requestedSession) {
      await resumeSessionFromUrl();
      return;
    }
    try {
      await loadSessions();
      if (!state && sessions.length) {
        const preferred = sessions.some((item) => item.id === lastSelectedSessionId) ? lastSelectedSessionId : sessions[0].id;
        await selectSession(preferred);
      }
    } catch (error) { fail(error); }
  }

  function revision() { return state?.session?.revision; }
  function isReviewReady() { return state?.session?.state === "ready_for_review" || state?.session?.state === "frozen"; }
  function isSharedFlow() { return state?.shared === true || state?.read_only === true; }
  function currentTrigger() {
    const keywords = state?.session?.flow_trigger_metadata?.keywords;
    return Array.isArray(keywords) && keywords.length ? String(keywords[0]?.value || "").trim() : "";
  }
  function compiledArtifact() {
    const pipeline = state?.pipeline;
    const stage = pipeline?.stages?.find((item) => item.name === "engine3_glific_artifact");
    return pipeline?.all_stages_passed && stage?.status === "passed" && stage?.json ? stage : null;
  }

  function beginFlowLoading({ sessionId = "", action = "flow", status = "Opening your flow…" } = {}) {
    flowLoadGeneration += 1;
    flowLoading = true;
    loadingSessionId = sessionId;
    loadingAction = action;
    loadingStatus = status;
    state = null;
    busy = true;
    workspaceMode = "publishing";
    processPanel = "conversation";
    publishingPhase = null;
    renderedPresentationSource = "";
    lastError = null;
    toastVisible = false;
    clearToastTimer();
    render();
    return flowLoadGeneration;
  }

  function finishFlowLoading() {
    flowLoading = false;
    loadingSessionId = "";
    loadingAction = "";
    loadingStatus = "";
    busy = false;
  }

  function flowLoadingHtml() {
    return "<div class=\"flow-loading\" role=\"status\" aria-live=\"polite\"><span class=\"spinner flow-loading-spinner\" aria-hidden=\"true\"></span><strong>" + esc(loadingStatus || "Opening your flow…") + "</strong><p>Your saved flow will appear here when it is ready.</p></div>";
  }

  function apply(payload, revealClarification = false, options = {}) {
    const requestedPanel = options?.processPanel || "";
    state = payload;
    cacheFlowPayload(payload);
    if (workspaceMode === "landing" && payload?.session) workspaceMode = "authoring";
    lastError = null;
    toastVisible = false;
    clearToastTimer();
    if (revealClarification) {
      queueClarificationReveal(payload);
    } else {
      clarificationRevealKey = "";
      clarificationRevealGeneration += 1;
    }
    threadRevealPending = Boolean(revealClarification);
    if (payload?.glific_publish || (payload?.pipeline && workspaceMode === "authoring")) {
      workspaceMode = "publishing";
      processPanel = requestedPanel || processPanel || "publishing";
    }
    if (requestedPanel) processPanel = requestedPanel;
    if (flowLoading) finishFlowLoading();
    if (payload?.session?.state !== "frozen") generating = false;
    render();
  }

  function fail(error) {
    showToast("error", friendlyError(error));
    const failedLoadAction = loadingAction;
    if (flowLoading) {
      flowLoadGeneration += 1;
      finishFlowLoading();
      state = null;
      workspaceMode = failedLoadAction.startsWith("landing-start") ? "landing" : "authoring";
      processPanel = "conversation";
      publishingPhase = null;
    }
    busy = false;
    render();
  }

  function setBusy(value) { busy = value; render(); }

  function renderAlerts() {
    const alerts = $("alerts");
    if (!alerts) return;
    if (!toastVisible || !lastError) { alerts.replaceChildren(); return; }
    alerts.innerHTML = "<div class=\"alert error\" role=\"alert\"><strong>" + esc(lastError.message) + "</strong><span>" + esc(lastError.recovery) + "</span><small>Reference: " + esc(lastError.reference || lastError.code || "AutoGlific") + "</small><button type=\"button\" class=\"alert-close\" aria-label=\"Dismiss notification\">×</button></div>";
  }

  function sessionStatus(item) {
    if (item.shared) return "Shared template · Review only";
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
      const loading = flowLoading && loadingSessionId === item.id;
      const active = state?.session?.id === item.id || loading;
      const keyword = Array.isArray(item.keywords) && item.keywords.length ? " · " + item.keywords[0] : "";
      const status = loading ? "Opening saved flow…" : sessionStatus(item) + (item.shared ? "" : keyword);
      return "<button type=\"button\" class=\"flow-item" + (active ? " active" : "") + (item.shared ? " shared" : "") + "\" data-session-id=\"" + esc(item.id) + "\" aria-current=\"" + String(active) + "\" aria-busy=\"" + String(loading) + "\"" + (flowLoading ? " disabled" : "") + "><strong>" + esc(item.title) + "</strong><span class=\"flow-item-status\">" + (loading ? "<span class=\"spinner\" aria-hidden=\"true\"></span>" : "") + esc(status) + "</span></button>";
    }).join("");
    const savedCount = sessions.length + " saved";
    $("flow-count").textContent = savedCount;
    $("flow-count-display").textContent = savedCount;
  }

  function renderShell() {
    if (hasAuthUi()) renderAuth();
    renderAccountMenu();
    $("app").dataset.view = workspaceMode;
    document.body.classList.toggle("landing-body", workspaceMode === "landing");
    const authBlocking = hasAuthUi() && authVisible && !authenticated;
    $("auth-view")?.classList.toggle("hidden", !authBlocking);
    $("landing-view").classList.toggle("hidden", workspaceMode !== "landing" || authBlocking);
    $("workspace-view").classList.toggle("hidden", workspaceMode === "landing" || authBlocking);
    $("authoring-view").classList.toggle("hidden", workspaceMode !== "authoring");
    $("review-view").classList.toggle("hidden", workspaceMode !== "review");
    $("process-view").classList.toggle("hidden", workspaceMode !== "publishing");
    $("flow-title").textContent = flowLoading ? "Opening flow…" : state?.session?.title || "Start a new flow";
    // Hosted deployments now use the native auth boundary, so New flow must
    // remain available and can route unauthenticated users to registration.
    $("new-flow").disabled = false;
    $("new-flow").setAttribute("aria-disabled", "false");
    $("save-flow").disabled = !state || busy || isSharedFlow();
    $("review-flow").disabled = !isReviewReady() || busy;
    $("review-flow").textContent = workspaceMode === "review" ? "Reviewing flow" : "Review flow";
    $("process-view").setAttribute("aria-busy", String(flowLoading));
    ["landing-start-hero", "landing-start-closing"].forEach((id) => {
      const button = $(id);
      if (!button) return;
      const active = flowLoading && loadingAction === id;
      button.disabled = busy || flowLoading;
      button.setAttribute("aria-busy", String(active));
      const status = $(id + "-status");
      if (status) status.textContent = active ? loadingStatus : "";
    });
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
    if (field === "capability" || field === "semantic.capability") return "Flow step: " + capabilityName(record.value);
    if (field === "statement" || field === "semantic.statement") return "Flow step: " + formatValue(record.value);
    if (field === "branch_target") return "Branch: " + branchLabel([record.value]);
    if (field === "flow.trigger_keywords") return "Starting words: " + formatValue(record.value);
    if (field === "input_type") return "Answer type: " + answerTypeLabel(record.value);
    if (field === "copy") return formatValue(record.value);
    if (field === "prompt") return "Question: " + formatValue(record.value);
    if (field === "title") return formatValue(record.value);
    if (field === "options") return "Choices: " + formatValue(record.value);
    if (field === "save_as") return "Answer name: " + formatValue(record.value);
    if (field === "source_variable") return "Saved answer: " + formatValue(record.value);
    if (field === "field_name") return "Contact field: " + formatValue(record.value);
    if (field === "capture_reference") return "Answer selected: " + formatValue(labelForStableValue(record.value));
    if (field === "reason") return "Completion: " + formatValue(record.value);
    return (FIELD_NAMES[field] || "Answer") + ": " + formatValue(record.value);
  }
  function recordPrompt(record) {
    if (record?.prompt) return String(record.prompt);
    return friendlyQuestion({ field_path: record?.field_path, answer_type: record?.answer_type }).heading;
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
    const nodeTurns = new Map();
    let lastTurn = null;
    const addTurn = (text, activeProposal = null) => {
      const source = sourceStatementKey(text);
      let turn = lastTurn;
      if (!turn || turn.text !== (source || "Untitled segment")) {
        turn = { key: activeProposal ? "proposal:" + activeProposal.id : "turn:" + turns.length, text: source || "Untitled segment", nodes: [], records: [], active: false };
        turns.push(turn);
        lastTurn = turn;
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
    if (flowLoading) return flowLoadingHtml();
    const entries = segmentEntries();
    if (!entries.length) return "<div class=\"empty-state\"><strong>Your flow statements will appear here</strong></div>";
    return entries.map((entry, index) => "<article class=\"segment" + (entry.current ? " current" : "") + "\"><span class=\"segment-number\">" + (index + 1) + "</span><div><p>" + esc(entry.text) + "</p>" + (entry.current ? "<span class=\"segment-state\">" + esc(entry.status) + "</span>" : "") + "</div></article>").join("");
  }
  function renderSegments() { $("segment-list").innerHTML = segmentsHtml(); }

  function conversationHtml() {
    if (flowLoading) return flowLoadingHtml();
    if (!state) return chatMessage("assistant", "I’ll help you build one segment at a time, ask for missing details, show the complete journey for review, and publish only after you approve it.", "AutoGlific");
    const session = state.session;
    const turns = conversationTurns();
    const messages = [];
    turns.forEach((turn) => {
      messages.push(chatMessage("user", esc(turn.text), "You"));
      const records = turn.records.filter((record) => fieldName(record) !== "validation");
      records.forEach((record) => {
        messages.push(chatMessage("assistant clarification", esc(recordPrompt(record)), "AutoGlific clarification"));
        messages.push(chatMessage("assistant decision", esc(answerSummary(record)), "AutoGlific decision"));
      });
      turn.nodes.forEach((node) => messages.push(chatMessage("assistant result", "<div class=\"step-confirmation\"><span class=\"step-detail\">" + nodeDisplay(node) + "</span><span class=\"recorded-inline\" role=\"img\" aria-label=\"Recorded\"><span class=\"step-check\" aria-hidden=\"true\">✓</span><span class=\"sr-only\">Recorded</span></span></div>", "AutoGlific")));
    });
    if (session.state === "waiting_for_answer" && state.current_question && !isValidationQuestion(state.current_question)) {
      const copy = friendlyQuestion(state.current_question);
      const explanation = copy.explanation ? "<p>" + esc(copy.explanation) + "</p>" : "";
      messages.push(chatMessage("assistant clarification current-question", "<strong>" + esc(copy.heading) + "</strong>" + explanation, "AutoGlific clarification"));
    }
    if (session.state === "ready_for_review" || session.state === "frozen") messages.push(chatMessage("assistant completion", "Flow complete. Review the flow before generating or publishing anything by clicking the Review flow button at the top.", "AutoGlific"));
    if (session.state === "blocked") messages.push(chatMessage("assistant question", esc(session.blocked_error?.message || "This flow needs attention before it can continue."), "AutoGlific"));
    if (busy && workspaceMode === "authoring") messages.push(chatMessage("assistant working", "<span class=\"working\"><span class=\"spinner\"></span>Working on this segment…</span>", "AutoGlific"));
    return messages.join("");
  }
  function renderConversation() {
    const thread = $("thread");
    thread.innerHTML = conversationHtml();
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
  function statusIcon(kind) {
    if (kind === "warning") return "<svg class=\"status-icon warning-icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M12 4 21 20H3L12 4Z\"></path><path d=\"M12 9v5m0 3h.01\"></path></svg>";
    return "<svg class=\"status-icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><rect x=\"5\" y=\"10\" width=\"14\" height=\"10\" rx=\"2\"></rect><path d=\"M8 10V7a4 4 0 0 1 8 0v3\"></path></svg>";
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
        apply(payload, true);
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
    if (isSharedFlow()) {
      $("instruction-form").classList.add("hidden");
      $("answer-form").classList.add("hidden");
      $("locked-composer").classList.add("hidden");
      return;
    }
    const instructionVisible = Boolean(state && workspaceMode === "authoring" && state.session.state === "editing");
    const lockedVisible = Boolean(state && workspaceMode === "authoring" && ["ready_for_review", "frozen"].includes(state.session.state));
    const blockedVisible = Boolean(state && workspaceMode === "authoring" && state.session.state === "blocked");
    $("instruction-form").classList.toggle("hidden", !instructionVisible);
    $("instruction").disabled = !instructionVisible || busy;
    $("instruction").placeholder = "What should happen first?";
    $("send-instruction").disabled = !instructionVisible || busy;
    $("composer-hint").textContent = !state ? "Create a named flow to begin" : state.session.state === "waiting_for_answer" ? "Your answer is used exactly as entered" : blockedVisible ? "Flow needs attention" : lockedVisible ? "Review is required before the next action" : "Please share your flow one branch/step at a time.";
    $("locked-composer").classList.toggle("hidden", !(lockedVisible || blockedVisible));
    if (lockedVisible) $("locked-composer").innerHTML = statusIcon("lock") + "<span>Flow complete — ready for review</span>";
    else if (blockedVisible) $("locked-composer").innerHTML = statusIcon("warning") + "<span>Flow needs attention. Review the message above, then choose this flow again or start a new one.</span>";
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
    const cachedSvg = mermaidSvgCache.get(renderKey);
    if (cachedSvg) {
      canvas.innerHTML = cachedSvg;
      renderedPresentationSource = renderKey;
      return;
    }
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
      mermaidSvgCache.set(renderKey, canvas.innerHTML);
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
    $("modify-flow").classList.toggle("hidden", isSharedFlow());
    $("confirm-flow").classList.toggle("hidden", isSharedFlow());
    $("modify-flow").disabled = busy || isSharedFlow();
    $("confirm-flow").disabled = busy || !isReviewReady() || isSharedFlow();
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
    const shared = isSharedFlow();
    ["download-json", "download-mermaid", "open-whatsapp"].forEach((id) => $(id)?.classList.toggle("hidden", shared));
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
    if (workspaceMode !== "publishing") return;
    $("process-view").dataset.openPanel = processPanel;
    renderTabState("process-tabs", processPanel, "panel");
    if (processPanel === "conversation") $("process-conversation-body").innerHTML = archiveConversationHtml();
    else if (!state) return;
    else if (processPanel === "mermaid") { renderedPresentationSource = ""; window.setTimeout(() => renderAuthoredMermaid(presentationMermaidSource(), "process-mermaid-graph"), 0); }
  }
  function render() {
    renderAlerts(); renderLibrary(); renderShell(); renderConversation(); renderSegments(); renderComposer(); renderReview(); renderProcessPanel(); renderPublishing();
    if (!threadRevealPending) return;
    threadRevealPending = false;
    const revealKey = clarificationRevealKey;
    const generation = clarificationRevealGeneration;
    window.setTimeout(() => {
      if (generation !== clarificationRevealGeneration) return;
      const thread = $("thread");
      if (thread) thread.scrollTop = thread.scrollHeight;
      if (revealKey && revealKey === questionControlKey(state?.current_question)) {
        clarificationRevealKey = "";
        renderQuestion();
      }
    }, 0);
  }

  async function loadSessions() {
    const generation = ++sessionsLoadGeneration;
    const namespace = currentCacheNamespace();
    const cached = libraryMemoryCache.get(namespace);
    if (cached) {
      sessions = orderSessions(Array.isArray(cached.sessions) ? cached.sessions : []);
      sessionsLoading = false;
      renderLibrary();
    } else {
      sessionsLoading = true;
      renderLibrary();
    }
    try {
      const payload = await request(authenticated ? "/api/sessions" : "/api/public/sessions");
      if (generation === sessionsLoadGeneration) {
        libraryMemoryCache.set(namespace, payload);
        sessions = orderSessions(Array.isArray(payload.sessions) ? payload.sessions : []);
      }
    }
    finally { if (generation === sessionsLoadGeneration) { sessionsLoading = false; renderLibrary(); } }
  }
  async function submitInstructionText(statement) {
    if (!state || isSharedFlow() || state.session.state !== "editing" || busy || !statement.trim()) return;
    setBusy(true);
    try { const payload = await request("/api/sessions/" + encodeURIComponent(state.session.id) + "/propose", { method: "POST", body: JSON.stringify({ revision: revision(), statement: statement.trim() }) }); $("instruction").value = ""; busy = false; apply(payload, true); void loadSessions(); }
    catch (error) { fail(error); }
  }
  async function createFlow(event) {
    event.preventDefault();
    const titleInput = $("flow-name");
    const title = titleInput.value.trim();
    if (!title) { $("name-error").textContent = "Enter a flow name to continue."; titleInput.focus(); return; }
    const suffix = Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 7);
    setBusy(true);
    try { const payload = await request("/api/sessions", { method: "POST", body: JSON.stringify({ session_id: "flow-" + suffix, title, reset: true }) }); workspaceMode = "authoring"; processPanel = "publishing"; publishingPhase = null; $("name-dialog").close(); titleInput.value = ""; $("name-error").textContent = ""; busy = false; window.history.replaceState({}, "", "?session=" + encodeURIComponent(payload.session.id)); apply(payload); void loadSessions(); window.setTimeout(() => $("instruction").focus(), 0); }
    catch (error) { fail(error); }
  }
  async function resumeSessionFromUrl() {
    const params = new URLSearchParams(window.location.search);
    if (hasAuthUi()) {
      await bootstrapAuth();
    }
    if (!params.has("session")) {
      if (hasAuthUi() && authenticated) {
        if (workspaceMode === "landing" && !state) void resumeAuthenticatedWorkspace();
        return;
      }
      workspaceMode = "landing";
      state = null;
      render();
      return;
    }
    const sessionId = (params.get("session") || "").trim();
    if (!sessionId) { const error = new Error("Session does not exist."); error.code = "P4_SESSION_NOT_FOUND"; fail(error); return; }
    if (hasAuthUi() && !authenticated) {
      try {
        const publicPayload = await request("/api/public/sessions/" + encodeURIComponent(sessionId));
        if (publicPayload.shared === true) {
          window.history.replaceState({}, "", "?session=" + encodeURIComponent(sessionId));
          workspaceMode = "authoring";
          void loadSessions().catch(() => {});
          apply(publicPayload, false);
          return;
        }
      } catch (error) {
        if (!["P4_AUTH_REQUIRED", "P4_AUTH_INVALID_SESSION"].includes(error?.code)) {
          fail(error);
          return;
        }
      }
      showAuth("login", "resume");
      return;
    }
    const generation = beginFlowLoading({ sessionId, action: "resume", status: "Opening your flow…" });
    void loadSessions().catch(() => {});
    try {
      const cached = flowMemoryCache.get(flowCacheKey(sessionId));
      if (cached) {
        if (generation !== flowLoadGeneration) return;
        workspaceMode = cached.glific_publish || cached.pipeline ? "publishing" : "authoring";
        processPanel = "conversation";
        validationAutoAnswerKey = "";
        renderedQuestionKey = "";
        apply(cached, true, { processPanel: "conversation" });
        void request(flowPath(sessionId)).then((fresh) => {
          if (generation === flowLoadGeneration && state?.session?.id === sessionId) apply(fresh, false, { processPanel: "conversation" });
        }).catch(() => {});
        return;
      }
      const payload = await request(flowPath(sessionId));
      if (generation !== flowLoadGeneration) return;
      workspaceMode = payload.glific_publish || payload.pipeline ? "publishing" : "authoring";
      processPanel = "conversation";
      validationAutoAnswerKey = "";
      renderedQuestionKey = "";
      apply(payload, true, { processPanel: "conversation" });
    }
    catch (error) { if (generation === flowLoadGeneration) fail(error); }
  }
  async function selectSession(sessionId, preferredProcessPanel = "conversation", existingGeneration = null) {
    if (!sessionId || (!existingGeneration && (busy || flowLoading))) return;
    closeAccountMenu();
    lastSelectedSessionId = sessionId;
    const generation = existingGeneration || beginFlowLoading({ sessionId, action: "saved-flow", status: "Opening saved flow…" });
    if (existingGeneration) {
      loadingSessionId = sessionId;
      loadingStatus = "Opening saved flow…";
      render();
    }
    try {
      const cached = flowMemoryCache.get(flowCacheKey(sessionId));
      if (cached) {
        if (generation !== flowLoadGeneration) return;
        workspaceMode = cached.glific_publish || cached.pipeline ? "publishing" : "authoring";
        processPanel = preferredProcessPanel;
        window.history.replaceState({}, "", "?session=" + encodeURIComponent(sessionId));
        validationAutoAnswerKey = "";
        renderedQuestionKey = "";
        apply(cached, true, { processPanel: preferredProcessPanel });
        void request(flowPath(sessionId)).then((fresh) => {
          if (generation === flowLoadGeneration && state?.session?.id === sessionId) apply(fresh, false, { processPanel: preferredProcessPanel });
        }).catch(() => {});
        return;
      }
      const payload = await request(flowPath(sessionId));
      if (generation !== flowLoadGeneration) return;
      workspaceMode = payload.glific_publish || payload.pipeline ? "publishing" : "authoring";
      processPanel = preferredProcessPanel;
      window.history.replaceState({}, "", "?session=" + encodeURIComponent(sessionId));
      validationAutoAnswerKey = "";
      renderedQuestionKey = "";
      apply(payload, true, { processPanel: preferredProcessPanel });
    }
    catch (error) { if (generation === flowLoadGeneration) fail(error); }
  }
  async function submitInstruction(event) { event.preventDefault(); await submitInstructionText($("instruction").value); }
  async function submitAnswer(event) {
    event.preventDefault();
    const question = state?.current_question;
    if (!question || busy) return;
    let value;
    try { value = readAnswer(question); } catch (error) { fail(error); return; }
    setBusy(true);
    try { const payload = await request("/api/sessions/" + encodeURIComponent(state.session.id) + "/answer", { method: "POST", body: JSON.stringify({ revision: revision(), question_id: question.id, value }) }); renderedQuestionKey = ""; busy = false; apply(payload, true); void loadSessions(); }
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
      apply(compiled); busy = false; render();
      if (compiled.pipeline?.all_stages_passed) await pushToGlific(true);
    } catch (error) { publishingPhase = "failed"; fail(error); }
  }
  async function compilePipeline() {
    if (!state || busy || state.session.state !== "frozen") return;
    workspaceMode = "publishing"; processPanel = "publishing"; publishingPhase = "compiling"; generating = true; setBusy(true);
    try { const payload = await request("/api/sessions/" + encodeURIComponent(state.session.id) + "/compile", { method: "POST", body: JSON.stringify({ revision: revision() }) }); generating = false; publishingPhase = payload.pipeline?.all_stages_passed ? null : "failed"; busy = false; apply(payload); }
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
    try { const payload = await request("/api/sessions/" + encodeURIComponent(state.session.id) + "/publish", { method: "POST", body: JSON.stringify({ revision: revision() }) }); stopGlificStatusPolling(); glificPublishing = false; publishingPhase = null; busy = false; apply(payload); if (!wasPublished && payload.glific_publish) launchConfetti(payload.session.id); }
    catch (error) { stopGlificStatusPolling(); glificPublishing = false; publishingPhase = "failed"; fail(error); if (hasAuthUi() && authenticated && error?.code === "P4_GLIFIC_CONFIGURATION_MISSING") void openSettings({ setup: true }); }
  }
  async function downloadArtifact(kind) {
    if (!state || isSharedFlow()) return;
    try { const link = document.createElement("a"); link.href = "/api/sessions/" + encodeURIComponent(state.session.id) + "/download/" + kind; link.download = kind === "glific" ? "glific.json" : kind === "presentation-mermaid" ? "presentation.mmd" : kind + ".json"; link.hidden = true; document.body.appendChild(link); link.click(); window.setTimeout(() => link.remove(), 1000); }
    catch (error) { fail(error); }
  }
  async function copyMermaid() { try { await navigator.clipboard.writeText(technicalAuthoredMermaidSource()); } catch (error) { fail(error); } }
  function resetActiveView() {
    stopGlificStatusPolling();
    state = null;
    busy = false;
    generating = false;
    glificPublishing = false;
    workspaceMode = "landing";
    processPanel = "publishing";
    reviewPanel = "mermaid";
    publishingPhase = null;
    validationAutoAnswerKey = "";
    renderedQuestionKey = "";
    clarificationRevealKey = "";
    clarificationRevealGeneration += 1;
    threadRevealPending = false;
    renderedPresentationSource = "";
    mermaidRenderError = null;
    flowLoadGeneration += 1;
    flowLoading = false;
    loadingSessionId = "";
    loadingAction = "";
    loadingStatus = "";
    lastError = null;
    toastVisible = false;
    clearToastTimer();
  }
  function goHome() {
    closeAccountMenu();
    resetActiveView();
    window.history.replaceState({}, "", window.location.pathname || "/");
    render();
  }
  function startNewFlow() {
    if (busy) return;
    closeAccountMenu();
    if (hasAuthUi() && !authenticated) {
      showAuth("register", "new-flow");
      return;
    }
    if (hasAuthUi() && settings?.authoring_configured !== true) {
      pendingAuthAction = "new-flow";
      void openSettings({ setup: true });
      return;
    }
    resetActiveView();
    workspaceMode = "authoring";
    window.history.replaceState({}, "", window.location.pathname || "/");
    render();
    openNameDialog();
    void loadSessions().catch((error) => fail(error));
  }
  async function startFromLanding(source = null) {
    if (busy || flowLoading) return;
    closeAccountMenu();
    const action = typeof source === "string" ? source : source?.currentTarget?.id || "landing-start";
    const generation = beginFlowLoading({ action, status: "Opening your saved flows…" });
    try {
      const knownSessionId = state?.session?.id || lastSelectedSessionId || "";
      const libraryRequest = loadSessions();
      if (knownSessionId) {
        await Promise.all([
          libraryRequest,
          selectSession(knownSessionId, "conversation", generation),
        ]);
        return;
      }
      await libraryRequest;
      if (generation !== flowLoadGeneration) return;
      if (!sessions.length) { finishFlowLoading(); startNewFlow(); return; }
      await selectSession(sessions[0].id, "conversation", generation);
    } catch (error) { if (generation === flowLoadGeneration) fail(error); }
  }
  function openNameDialog() {
    if (busy) return;
    closeAccountMenu();
    if (hasAuthUi() && !authenticated) {
      showAuth("register", "new-flow");
      return;
    }
    if (hasAuthUi() && settings?.authoring_configured !== true) {
      pendingAuthAction = "new-flow";
      void openSettings({ setup: true });
      return;
    }
    if ($("name-error")) $("name-error").textContent = "";
    $("name-dialog").showModal();
    window.setTimeout(() => $("flow-name").focus(), 0);
  }
  function clearSettingsUi() {
    wizardStep = 1;
    ["openai-api-key", "openai-project-id", "glific-url", "mobile-number", "glific-password"].forEach((id) => {
      if ($(id)) $(id).value = "";
    });
    document.querySelectorAll("#settings-form input[type=checkbox]").forEach((input) => { input.checked = false; });
    if ($("settings-status")) $("settings-status").textContent = "";
    if ($("settings-details")) $("settings-details").innerHTML = "";
    renderWizardStep();
  }

  function wizardField(step) {
    return {
      1: "glific-url",
      2: "mobile-number",
      3: "glific-password",
      4: "openai-api-key",
    }[step] || "";
  }

  function renderWizardStep() {
    const stepSections = Array.from(document.querySelectorAll?.("[data-wizard-step]") || []);
    stepSections.forEach((section) => {
      const active = Number(section.dataset?.wizardStep) === wizardStep;
      section.classList.toggle("hidden", !active);
    });
    const progress = Array.from(document.querySelectorAll?.("[data-wizard-progress]") || []);
    progress.forEach((item) => {
      const number = Number(item.dataset?.wizardProgress);
      item.classList.toggle("current", number === wizardStep);
      item.classList.toggle("complete", number < wizardStep);
      item.setAttribute("aria-current", number === wizardStep ? "step" : "false");
    });
    $("settings-back")?.classList.toggle("hidden", wizardStep <= 1);
    $("settings-back")?.toggleAttribute?.("disabled", wizardStep <= 1);
    $("settings-next")?.classList.toggle("hidden", wizardStep >= 5);
    $("settings-save")?.classList.toggle("hidden", wizardStep !== 5);
    if (wizardStep === 5) renderSettingsReview();
  }

  function renderSettingsReview() {
    if (!settings || !$("settings-details")) return;
    const openai = settings.openai_api_key || {};
    const projectId = settings.openai_project_id || "Not configured";
    const mobile = settings.mobile_number || {};
    const password = settings.password || {};
    $("settings-details").innerHTML = "<dt>OpenAI API key</dt><dd>" + esc(openai.masked || "Not configured") + "</dd><dt>OpenAI project ID</dt><dd>" + esc(projectId) + "</dd><dt>Glific URL</dt><dd>" + esc(settings.glific_url || "Not configured") + "</dd><dt>Mobile number</dt><dd>" + esc(mobile.masked || "Not configured") + "</dd><dt>Glific password</dt><dd>" + esc(password.masked || "Not configured") + "</dd><dt>Runtime model</dt><dd>" + esc(settings.model || "gpt-5.6-sol") + "</dd>";
  }

  function validateWizardStep() {
    const status = $("settings-status");
    if (status) status.textContent = "";
    const value = $(wizardField(wizardStep))?.value.trim() || "";
    const clearId = { 1: "clear-glific-base-url", 2: "clear-mobile-number", 3: "clear-glific-password", 4: "clear-openai-api-key" }[wizardStep];
    const clear = $(clearId)?.checked === true;
    if (wizardStep === 1 && value) {
      try {
        const url = new URL(value);
        if (url.protocol !== "https:" || !url.hostname) throw new Error("invalid");
      } catch (_error) {
        if (status) status.textContent = "Use a valid HTTPS Glific base URL.";
        return false;
      }
    }
    if (wizardStep === 2 && value) {
      if (!/^[+0-9()\-\s]{3,64}$/.test(value) || (value.match(/[0-9]/g) || []).length < 3) {
        if (status) status.textContent = "Enter a mobile number with at least three digits.";
        return false;
      }
    }
    if (wizardStep === 3 && value && value.length > 512) {
      if (status) status.textContent = "The Glific password is too long.";
      return false;
    }
    if (wizardStep === 4 && !value && !clear && settings?.openai_api_key?.configured !== true) {
      if (status) status.textContent = "An OpenAI API key is required before authoring.";
      return false;
    }
    if (wizardStep === 4) {
      const projectId = $("openai-project-id")?.value.trim() || "";
      if (projectId && !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(projectId)) {
        if (status) status.textContent = "Use a valid OpenAI project ID, up to 128 letters, numbers, hyphens, or underscores.";
        return false;
      }
    }
    return true;
  }

  function advanceWizard() {
    if (!validateWizardStep()) return;
    wizardStep = Math.min(5, wizardStep + 1);
    renderWizardStep();
    const field = wizardField(wizardStep);
    if (field) window.setTimeout(() => $(field)?.focus(), 0);
  }

  function retreatWizard() {
    wizardStep = Math.max(1, wizardStep - 1);
    renderWizardStep();
    const field = wizardField(wizardStep);
    if (field) window.setTimeout(() => $(field)?.focus(), 0);
  }

  async function openSettings(options = {}) {
    closeAccountMenu();
    if (hasAuthUi() && !authenticated) {
      showAuth("login", "settings");
      return;
    }
    setupMode = Boolean(options.setup);
    wizardStep = 1;
    const dialog = $("settings-dialog");
    if (!dialog) return;
    clearSettingsUi();
    dialog.showModal();
    if ($("settings-title")) $("settings-title").textContent = setupMode ? "Connect your accounts" : "Account settings";
    if ($("settings-intro")) $("settings-intro").textContent = setupMode ? "OpenAI is required for authoring. Glific is optional until you publish." : "Replace or clear saved provider credentials. Blank replace fields keep existing values.";
    if ($("settings-skip")) $("settings-skip").textContent = "Cancel";
    settings = null;
    if ($("settings-details")) $("settings-details").innerHTML = "<dt>Status</dt><dd>Loading secure connection status…</dd>";
    if ($("settings-status")) $("settings-status").textContent = "";
    try {
      settings = await request("/api/settings");
      if (setupMode && settings.authoring_configured === true && settings.glific_configured === true) {
        closeSettingsDialog({ resume: true });
        return;
      }
      renderSettings();
    }
    catch (error) { if ($("settings-details")) $("settings-details").innerHTML = "<dt>Status</dt><dd>Settings could not be loaded. " + esc(friendlyError(error).recovery) + "</dd>"; }
  }
  function renderSettings() {
    if (!settings) return;
    if ($("settings-form")) {
      if ($("glific-url")) $("glific-url").value = settings.glific_url || "";
      const openai = settings.openai_api_key || {};
      if ($("openai-project-id")) $("openai-project-id").value = settings.openai_project_id || "";
      const glific = settings.glific_configured ? "configured" : "not configured";
      if ($("settings-status")) $("settings-status").textContent = "OpenAI API key: " + (openai.configured ? "configured (masked)" : "not configured") + " · Project ID: " + (settings.openai_project_id ? "configured" : "not configured") + " · Glific: " + glific + " · Runtime model: " + String(settings.model || "gpt-5.6-sol");
      renderSettingsReview();
      renderWizardStep();
      return;
    }
    // Compatibility path for the small legacy UI runtime harness.
    $("settings-details").innerHTML = "<dt>Glific URL</dt><dd>" + esc(settings.glific_url || "Not configured") + "</dd><dt>Mobile number</dt><dd>" + esc(settings.mobile_number || "Not configured") + "</dd><dt>Password</dt><dd>" + esc(settings.password || "Not configured") + "</dd>";
  }
  async function saveSettings(event) {
    event.preventDefault();
    if (!hasAuthUi() || !authenticated || busy) return;
    if (wizardStep < 5) {
      advanceWizard();
      return;
    }
    if (!validateWizardStep()) return;
    const body = {};
    const values = [["openai_api_key", "openai-api-key"], ["openai_project_id", "openai-project-id"], ["glific_base_url", "glific-url"], ["mobile_number", "mobile-number"], ["glific_password", "glific-password"]];
    values.forEach(([field, id]) => { const value = $(id)?.value.trim(); if (value) body[field] = value; });
    ["openai_api_key", "openai_project_id", "mobile_number", "glific_password", "glific_base_url"].forEach((field) => { const checkbox = $("clear-" + field.replaceAll("_", "-")); if (checkbox?.checked) body["clear_" + field] = true; });
    busy = true;
    if ($("settings-status")) $("settings-status").textContent = "Saving encrypted settings…";
    try {
      settings = await request("/api/settings", { method: "POST", body: JSON.stringify(body) });
      ["openai-api-key", "openai-project-id", "mobile-number", "glific-password"].forEach((id) => { if ($(id)) $(id).value = ""; });
      document.querySelectorAll("#settings-form input[type=checkbox]").forEach((input) => { input.checked = false; });
      busy = false;
      renderSettings();
      if (setupMode && !settings.authoring_configured && $("settings-status")) $("settings-status").textContent = "Save an OpenAI API key before creating a flow.";
      else {
        const wasSetup = setupMode;
        closeSettingsDialog({ resume: wasSetup });
      }
    } catch (error) {
      busy = false;
      if ($("settings-status")) $("settings-status").textContent = friendlyError(error).message + " " + friendlyError(error).recovery;
    }
  }
  function closeSettingsDialog({ resume = false } = {}) {
    if ($("settings-dialog")) $("settings-dialog").close();
    if (!resume) pendingAuthAction = "";
    setupMode = false;
    if (resume) resumePendingAuthAction();
  }
  async function logout() {
    if (!authenticated || logoutInProgress) return;
    logoutInProgress = true;
    const generation = ++logoutGeneration;
    closeAccountMenu();
    renderAccountMenu();
    let result;
    try {
      try {
        result = await request("/api/auth/logout", { method: "POST", body: "{}" });
      } catch (error) {
        if (error?.code !== "P4_CSRF_REQUIRED") throw error;
        const csrf = await request("/api/auth/csrf");
        csrfToken = String(csrf.csrf_token || "");
        result = await request("/api/auth/logout", { method: "POST", body: "{}" });
      }
    } catch (error) {
      if (generation !== logoutGeneration) return;
      logoutInProgress = false;
      renderAccountMenu();
      const message = error?.code === "P4_AUTH_INVALID_SESSION" || error?.code === "P4_AUTH_REQUIRED"
        ? "You are already signed out."
        : "Sign out could not be completed. Try again when the connection is available.";
      if (error?.code === "P4_AUTH_INVALID_SESSION" || error?.code === "P4_AUTH_REQUIRED") {
        authenticated = false;
        authUser = null;
        clearClientCaches();
        logoutInProgress = false;
        render();
        return;
      }
      if ($("logout-status")) $("logout-status").textContent = message;
      showToast("error", { code: error?.code || "P4_AUTH_LOGOUT_FAILED", message, recovery: "Your account is still signed in." });
      return;
    }
    if (generation !== logoutGeneration) return;
    // The server has cleared the HttpOnly session cookie. Clear the UI now;
    // refreshing an anonymous CSRF token must not hold up sign-out.
    authenticated = false;
    authUser = null;
    clearClientCaches();
    csrfToken = "";
    settings = null;
    sessions = [];
    sessionsLoading = false;
    authVisible = false;
    setupMode = false;
    accountMenuOpen = false;
    $("settings-dialog")?.close();
    clearSettingsUi();
    resetActiveView();
    window.history.replaceState({}, "", window.location.pathname || "/");
    logoutInProgress = false;
    render();
    void request("/api/auth/csrf").then((csrf) => {
      if (generation === logoutGeneration) csrfToken = String(csrf.csrf_token || "");
    }).catch(() => {});
  }
  function openReviewPanel(panel) { if (state && workspaceMode === "review" && ["conversation", "mermaid"].includes(panel)) { closeAccountMenu(); reviewPanel = panel; if (panel === "mermaid") invalidateMermaidRender(); render(); } }
  function openProcessPanel(panel) { if (state && workspaceMode === "publishing") { closeAccountMenu(); processPanel = panel; renderedPresentationSource = ""; render(); } }

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
      startNewFlow,
      startFromLanding,
      openSettings,
      showAuth,
      submitAuth,
      toggleAccountMenu,
      closeAccountMenu,
      logout,
      renderAccountMenu,
      setAuth({ authenticated: nextAuthenticated = false, user = null } = {}) {
        authenticated = Boolean(nextAuthenticated);
        authUser = user;
        clearClientCaches();
        authVisible = false;
        accountMenuOpen = false;
        render();
      },
      goHome,
      resumeSessionFromUrl,
      showToast,
      stableChoiceValues,
      renderOptionEditor,
      conversationHtml,
      segmentEntries,
      archiveConversationHtml,
      renderLibrary,
      selectSession,
      getUiState() { return { busy, flowLoading, loadingSessionId, loadingAction, loadingStatus, workspaceMode, processPanel, hasState: Boolean(state) }; },
      setSessions(items) { sessionsLoading = false; sessions = orderSessions(items || []); renderLibrary(); },
    };
  }

  $("flow-list").addEventListener("click", (event) => { const button = event.target.closest("[data-session-id]"); if (button) void selectSession(button.dataset.sessionId); });
  $("landing-start-hero").addEventListener("click", startFromLanding);
  $("landing-start-closing").addEventListener("click", startFromLanding);
  $("new-flow").addEventListener("click", openNameDialog);
  $("name-form").addEventListener("submit", createFlow);
  $("cancel-name").addEventListener("click", () => $("name-dialog").close());
  $("home-button").addEventListener("click", goHome);
  $("account-menu-trigger")?.addEventListener("click", toggleAccountMenu);
  $("account-menu-panel")?.addEventListener("keydown", moveAccountMenuFocus);
  $("settings-menu")?.addEventListener("click", () => void openSettings());
  $("close-settings").addEventListener("click", closeSettingsDialog);
  $("settings-dialog")?.addEventListener("cancel", (event) => { event.preventDefault(); closeSettingsDialog(); });
  $("settings-skip")?.addEventListener("click", closeSettingsDialog);
  $("settings-back")?.addEventListener("click", retreatWizard);
  $("settings-next")?.addEventListener("click", advanceWizard);
  $("settings-form")?.addEventListener("submit", saveSettings);
  $("logout-menu")?.addEventListener("click", () => void logout());
  $("auth-form")?.addEventListener("submit", submitAuth);
  $("auth-switch")?.addEventListener("click", () => { authMode = authMode === "register" ? "login" : "register"; if ($("auth-display-name")) $("auth-display-name").value = ""; if ($("auth-password")) $("auth-password").value = ""; if ($("auth-error")) $("auth-error").textContent = ""; renderAuth(); });
  $("auth-back")?.addEventListener("click", () => { authVisible = false; pendingAuthAction = ""; render(); });
  $("alerts").addEventListener("click", (event) => { if (event.target.closest(".alert-close")) dismissToast(); });
  $("instruction-form").addEventListener("submit", submitInstruction);
  $("answer-form").addEventListener("submit", submitAnswer);
  $("review-flow").addEventListener("click", () => { if (isReviewReady() && !busy) { workspaceMode = "review"; invalidateMermaidRender(); render(); } });
  $("modify-flow").addEventListener("click", () => { if (isSharedFlow()) return; workspaceMode = "authoring"; renderedPresentationSource = ""; render(); window.setTimeout(() => $("instruction").focus(), 0); });
  $("confirm-flow").addEventListener("click", () => void confirmFlow());
  $("review-tabs").addEventListener("click", (event) => { const button = event.target.closest("[data-review-panel]"); if (button) openReviewPanel(button.dataset.reviewPanel); });
  $("review-tabs").addEventListener("keydown", (event) => moveTabFocus(event, "review-tabs", "reviewPanel"));
  $("process-view").addEventListener("click", (event) => { const button = event.target.closest("[data-panel]"); if (button) openProcessPanel(button.dataset.panel); });
  $("process-tabs").addEventListener("keydown", (event) => moveTabFocus(event, "process-tabs", "panel"));
  $("pipeline-retry").addEventListener("click", () => void compilePipeline());
  $("publication-retry").addEventListener("click", () => void pushToGlific());
  $("download-json").addEventListener("click", () => void downloadArtifact("glific"));
  $("download-mermaid").addEventListener("click", () => void downloadArtifact("presentation-mermaid"));
  document.addEventListener("click", (event) => {
    if (!accountMenuOpen) return;
    const target = event.target;
    if (!target?.closest || !target.closest("#account-menu")) closeAccountMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && accountMenuOpen) {
      event.preventDefault();
      closeAccountMenu({ restoreFocus: true });
    }
  });
  void resumeSessionFromUrl();
})();
