/**
 * spark-api.js — Shared API client for Spark frontend
 * Handles: JWT auth storage, REST calls, Socket.IO connection
 *
 * PAYMENT FLOW (card only, tokens credited by webhook):
 *   1. buyTokens(pack_id) → POST /api/payments/create-checkout
 *   2. Redirect user to Stripe hosted checkout page (card only)
 *   3. Stripe calls our webhook → tokens added ONLY after confirmed payment
 *   4. payment-success.html polls /api/payments/verify-session/:id for confirmation
 */

var SparkAPI = window.SparkAPI = (() => {

  // ── Token storage ──────────────────────────────────────────────────────────
  function getToken()    { return localStorage.getItem("spark_token"); }
  function setToken(t)   { localStorage.setItem("spark_token", t); }
  function removeToken() { localStorage.removeItem("spark_token"); }
  function isLoggedIn()  { return !!getToken(); }

  function authHeaders() {
    const t = getToken();
    return t
      ? { "Authorization": "Bearer " + t, "Content-Type": "application/json" }
      : { "Content-Type": "application/json" };
  }

  // ── Generic fetch wrapper ──────────────────────────────────────────────────
  async function req(method, path, body) {
    const opts = { method, headers: authHeaders() };
    if (body) opts.body = JSON.stringify(body);
    const res  = await fetch(path, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(
      new Error(data.error || "Request failed"),
      { status: res.status, data }
    );
    return data;
  }

  // ── Auth ───────────────────────────────────────────────────────────────────
  async function register(first_name, last_name, email, password, age, gender) {
    const data = await req("POST", "/api/auth/register", { first_name, last_name, email, password, age, gender });
    if (data.token) setToken(data.token);
    return data;
  }

  async function login(email, password) {
    const data = await req("POST", "/api/auth/login", { email, password });
    setToken(data.token);
    return data;
  }

  function logout() { removeToken(); }

  async function verifyEmail(email, code) {
    return req("POST", "/api/auth/verify-email", { email, code });
  }

  async function resendVerification(email) {
    return req("POST", "/api/auth/resend-verification", { email });
  }

  async function me()                    { return req("GET",   "/api/auth/me"); }
  async function tokenBalance()          { return req("GET",   "/api/auth/tokens"); }
  async function updateProfile(fields)   { return req("PATCH", "/api/auth/me", fields); }
  async function changePassword(cur, nw) {
    return req("POST", "/api/auth/change-password", { current_password: cur, new_password: nw });
  }

  // ── Payments ───────────────────────────────────────────────────────────────
  async function listPacks()            { return req("GET",  "/api/payments/packs"); }
  async function purchaseHistory()      { return req("GET",  "/api/payments/history"); }
  async function deductTokens(amount)   { return req("POST", "/api/payments/deduct", { amount }); }
  async function verifySession(sid)     { return req("GET",  `/api/payments/verify-session/${sid}`); }
  async function grantTokensDev(amount) { return req("POST", "/api/payments/grant-tokens", { amount }); }
  async function cashoutSummary()       { return req("GET",  "/api/payments/cashout/summary"); }
  async function requestCashout(amount_cents, method, destination) {
    return req("POST", "/api/payments/cashout/request", { amount_cents, method, destination });
  }

  async function sendGift(gift_type, session_id) {
    return req("POST", "/api/payments/send-gift", { gift_type, session_id });
  }

  /**
   * buyTokens — the ONLY way to purchase tokens.
   * Creates a Stripe Checkout session (card only) and redirects the user.
   * Tokens are credited ONLY after Stripe fires the webhook.
   */
  async function buyTokens(pack_id) {
    if (!isLoggedIn()) {
      window.location.href = "/login.html";
      return;
    }
    try {
      const data = await req("POST", "/api/payments/create-checkout", { pack_id });
      if (data.checkout_url) {
        // Full redirect to Stripe hosted checkout page
        window.location.href = data.checkout_url;
      } else {
        alert("Could not start checkout. Please try again.");
      }
    } catch (e) {
      alert(e.message || "Checkout failed. Please try again.");
    }
  }

  // ── Chat sessions ──────────────────────────────────────────────────────────
  async function startSession(type = "video") {
    return req("POST", "/api/chat/session/start", { type });
  }
  async function endSession(session_id) {
    return req("POST", `/api/chat/session/${session_id}/end`);
  }
  async function saveMessage(session_id, content, type = "text") {
    return req("POST", `/api/chat/session/${session_id}/message`, { content, type });
  }
  async function getMessages(session_id) {
    return req("GET", `/api/chat/session/${session_id}/messages`);
  }
  async function mySessions(page = 1) {
    return req("GET", `/api/chat/sessions?page=${page}`);
  }

  // ── Reports ────────────────────────────────────────────────────────────────
  async function submitReport(reason, session_id, notes = "") {
    return req("POST", "/api/users/report", { reason, session_id, notes });
  }

  // ── TURN / ICE ────────────────────────────────────────────────────────────
  async function turnCredentials() {
    return req("GET", "/api/users/turn-credentials");
  }

  // ── Socket.IO ─────────────────────────────────────────────────────────────
  let _socket = null;

  function connectSocket() {
    if (_socket) return _socket;
    // Token sent after every connect/reconnect, not in query string.
    _socket = io({
      transports: ["polling", "websocket"],
      reconnection: true,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 5000,
      reconnectionAttempts: Infinity,
      timeout: 20000,
    });
    _socket.on("connect", () => {
      _socket.emit("authenticate", { token: getToken() || "" });
    });
    _socket.on("disconnect", (reason) => {
      if (reason === "io server disconnect") _socket.connect();
    });
    return _socket;
  }

  function getSocket() { return _socket; }

  // ── Expose ─────────────────────────────────────────────────────────────────
  return {
    // auth
    register, login, logout, verifyEmail, resendVerification, me, tokenBalance, updateProfile, changePassword,
    isLoggedIn, getToken,
    // payments (card only, webhook-confirmed)
    listPacks, purchaseHistory, deductTokens, verifySession,
    sendGift, buyTokens, grantTokensDev,
    cashoutSummary, requestCashout,
    // chat
    startSession, endSession, saveMessage, getMessages, mySessions,
    // reports
    submitReport, turnCredentials,
    // socket
    connectSocket, getSocket,
  };
})();

// ── Auto-refresh token badge on any page that has #tokenCount ─────────────────
(async () => {
  const badge = document.getElementById("tokenCount");
  if (!badge || !SparkAPI.isLoggedIn()) return;
  try {
    const { tokens } = await SparkAPI.tokenBalance();
    badge.textContent = tokens;
    const parent = badge.closest(".token-badge");
    if (parent) parent.classList.toggle("low", tokens <= 5);
  } catch (_) {}
})();
