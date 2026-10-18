/* The Setup screen, #/autopilot/setup (design §9, §15, task 12.5;
   Requirements 11.1-11.8, 5.6, N5.1, N5.2).

   - The readiness checklist (11.1): required items keep publishing to their
     platform off until they're done, with a fix-it step each (11.2);
     advisory items say what happens without them (11.3). Each fix-it step
     takes the user to the control that fixes it.
   - YouTube: "Connect for Autopilot" with the permission it asks for and why
     (11.8); adding the Google OAuth client on this Mac; the connection's
     details (11.5); the channel audience, which Autopilot never guesses
     (4.7); the two advisory declarations; disconnecting (11.7).
   - Instagram: the Meta app settings and the Meta-side steps, Connect
     Instagram (Facebook Login for Business), the Page picker, the
     paste-a-token fallback, what's missing when Meta refuses (11.4), the
     connection's details and disconnecting.
   - The browser and password logins stay for manual uploads, and the screen
     says why Autopilot never uses them (11.6).
   - The time zone posting times are read in (5.6).
   Both sign-ins leave the page and come back to /?youtube=… or
   /?instagram=…; the router turns that into this screen and leaves the
   outcome in sessionStorage under SIGNIN_RETURN_KEY. */
import { api } from "./api.js";
import { modal } from "./dialog.js";
import { el, field, icon, setError, sr } from "./dom.js";
import { formatTime, plural, zoneLongName } from "./format.js";
import { say } from "./live.js";
import { browserZone, prefs, savePrefs } from "./prefs.js";
import { SIGNIN_RETURN_KEY } from "./router.js";

const GOOGLE_CONSOLE = "https://console.cloud.google.com/";
const META_APPS = "https://developers.facebook.com/apps/";
const GRAPH_EXPLORER = "https://developers.facebook.com/tools/explorer/";
const SCOPE_NAMES = {
  "https://www.googleapis.com/auth/youtube": "Manage your YouTube videos",
  "https://www.googleapis.com/auth/youtube.upload": "Upload YouTube videos",
  "https://www.googleapis.com/auth/youtube.readonly": "View your YouTube account",
};
const COMMANDS = {
  text_shaping: "Run this in Terminal, then restart Clip Maker:",
  face_detector: "Run this in Terminal in the Clip Maker folder, then restart Clip Maker:",
};

function zones() {
  try {
    return Intl.supportedValuesOf("timeZone");
  } catch {
    return [];
  }
}

const link = (href, text) => el("a", { href, target: "_blank", rel: "noopener noreferrer",
                                       class: "ap-textlink" }, text, sr(" (opens in a new tab)"));

function details(rows) {
  const dl = el("dl", { class: "ap-details" });
  for (const [term, value] of rows) {
    if (value === null || value === undefined || value === "") continue;
    dl.append(el("div", {}, el("dt", {}, term), el("dd", {}, value)));
  }
  return dl;
}

function callout(tone, iconName, ...content) {
  return el("div", { class: `ap-q-callout ${tone}` }, icon(iconName), el("div", {}, ...content));
}

export async function render(container) {
  const state = { readiness: null, youtube: null, instagram: null, igLogin: null,
                  zone: null, busy: false, returned: null };
  try {
    state.returned = JSON.parse(sessionStorage.getItem(SIGNIN_RETURN_KEY) || "null");
    sessionStorage.removeItem(SIGNIN_RETURN_KEY);
  } catch {
    state.returned = null;
  }

  const heading = el("h1", { class: "ap-h1" }, "Setup");
  const lead = el("p", { class: "ap-lead" },
                  "Work down the list. Once a platform's required items are done, Autopilot can publish " +
                  "there. It connects only through YouTube's and Instagram's official sign-in, never with " +
                  "your password.");
  const notice = el("div", { class: "ap-setup-notice" });
  const problem = el("p", { class: "ap-problem", role: "alert", hidden: true });
  const body = el("div", { class: "ap-setup" });
  container.append(heading, lead, notice, problem, body);

  function showProblem(text) {
    problem.textContent = text || "";
    problem.hidden = !text;
  }

  async function load() {
    const [readiness, youtube, instagram, igLogin, stored] = await Promise.allSettled([
      api.get("/api/autopilot/readiness"), api.get("/api/connections/youtube"),
      api.get("/api/connections/instagram"), api.get("/api/instagram/status"), prefs({ fresh: true })]);
    const failed = [readiness, youtube, instagram].find(r => r.status === "rejected");
    if (failed) {
      showProblem(`Couldn't read the setup: ${failed.reason.message}`);
      return false;
    }
    state.readiness = readiness.value;
    state.youtube = youtube.value;
    state.instagram = instagram.value;
    state.igLogin = igLogin.status === "fulfilled" ? igLogin.value : null;
    state.zone = stored.status === "fulfilled" ? stored.value.timezone : null;
    showProblem("");
    return true;
  }

  const when = t => (t ? formatTime(t, state.zone || browserZone(), { year: true }) : null);

  /* ── the checklist ───────────────────────────────────────────────────── */
  const FIX_TARGET = {
    youtube_connected: "yt-connect", youtube_audience: "yt-audience", instagram_connected: "ig-connect",
    oauth_production: "yt-oauth", youtube_audit: "yt-audit", timezone: "tz",
  };

  // A disabled button can't take focus: go to what has to be done first.
  const BEFORE = { "yt-connect": "yt-client", "ig-connect": "ig-appid" };

  function goTo(key) {
    let target = body.querySelector(`[data-focus="${CSS.escape(key)}"]`);
    if (target && target.disabled && BEFORE[key]) {
      target = body.querySelector(`[data-focus="${CSS.escape(BEFORE[key])}"]`) || target;
    }
    if (!target) return;
    target.scrollIntoView({ block: "center" });
    target.focus({ preventScroll: true });
  }

  function fixFor(item) {
    if (item.ok || !item.fix) return null;
    if (item.key === "ai_engine") {
      const chip = document.getElementById("aiChip");
      if (!chip) return el("p", { class: "ap-ck-fix" }, item.fix);
      const button = el("button", { type: "button", class: "ap-btn ghost sm" }, item.fix);
      button.addEventListener("click", () => chip.click());
      return button;
    }
    if (COMMANDS[item.key]) {
      return el("div", { class: "ap-ck-fix" }, el("span", {}, COMMANDS[item.key]), " ",
                el("code", { class: "ap-code" }, item.fix));
    }
    if (item.key === "youtube_audit") {
      return el("div", { class: "ap-ck-fix" }, link(item.fix, "Apply for YouTube's API audit"),
                ", then tick the box under YouTube.");
    }
    const target = FIX_TARGET[item.key];
    if (!target) return el("p", { class: "ap-ck-fix" }, item.fix);
    const button = el("button", { type: "button", class: "ap-btn ghost sm" }, item.fix);
    button.addEventListener("click", () => goTo(target));
    return button;
  }

  function checklist() {
    const { items, platforms } = state.readiness;
    const summary = el("div", { class: "ap-ready" });
    for (const platform of ["youtube", "instagram"]) {
      const name = platform === "youtube" ? "YouTube" : "Instagram";
      const info = platforms[platform] || { ready: false, reasons: [] };
      summary.append(el("p", { class: `ap-ready-line ${info.ready ? "ok" : "off"}`, "data-platform": platform },
                        icon(info.ready ? "check" : "cross"),
                        info.ready ? `${name}: Autopilot can publish.`
                                   : `${name}: publishing is off until ${plural(info.reasons.length, "required item")} ` +
                                     `${info.reasons.length === 1 ? "is" : "are"} done.`));
    }
    const groups = [["required", "Required"], ["advisory", "Advisory"], ["setting", "Setting"]];
    const list = el("ul", { class: "ap-checklist", "aria-label": "Readiness checklist" });
    for (const [kind, title] of groups) {
      for (const item of items.filter(i => i.kind === kind)) {
        const status = item.kind === "setting" ? (item.ok ? "Set" : "Not set")
          : item.ok ? "Done" : item.kind === "required" ? "Needed" : "Recommended";
        const tone = item.ok ? "ok" : item.kind === "required" ? "err" : "warn";
        list.append(el("li", { class: `ap-ck ${tone}`, "data-key": item.key },
                       icon(item.ok ? "check" : item.kind === "required" ? "cross" : "alert", "ap-ck-ico"),
                       el("div", { class: "ap-ck-text" },
                          el("p", { class: "ap-ck-label" }, el("b", {}, item.label),
                             el("span", { class: `ap-pill ${tone}` }, status),
                             el("span", { class: "ap-ck-kind" }, title)),
                          el("p", { class: "ap-ck-msg" }, item.message),
                          fixFor(item))));
      }
    }
    return el("section", { class: "ap-card", "aria-labelledby": "ap-setup-check" },
              el("h2", { id: "ap-setup-check" }, "Checklist"), summary, list);
  }

  /* ── YouTube ──────────────────────────────────────────────────────────── */
  function permissionList(permissions, tag) {
    return el("ul", { class: "ap-perms" }, ...permissions.map(p => {
      const name = SCOPE_NAMES[p.scope] || p.scope;
      // The server's text may start with the name ("Manage your YouTube videos: needed to …").
      const why = p.why.startsWith(`${name}: `)
        ? p.why[name.length + 2].toUpperCase() + p.why.slice(name.length + 3) : p.why;
      return el("li", {}, el("b", {}, name), tag ? tag(p) : null, el("span", {}, why));
    }));
  }

  function youtubeSection() {
    const yt = state.youtube;
    const account = yt.account;
    const lost = account && account.status === "needs_reconnect";
    const forAutopilot = yt.connected && yt.autopilot && account && account.status === "ok";
    const section = el("section", { class: "ap-card", "aria-labelledby": "ap-setup-yt" },
                       el("h2", { id: "ap-setup-yt" }, "YouTube"));

    let status;
    if (lost) status = callout("err", "alert", account.status_reason || "YouTube access was lost. Reconnect to continue.");
    else if (forAutopilot) status = el("p", { class: "ap-conn ok" }, icon("check"), `Connected to ${account.name || "your channel"} for Autopilot.`);
    else if (yt.connected) status = el("p", { class: "ap-conn warn" }, icon("alert"),
      "Connected for manual uploads only. Autopilot needs one more permission: Manage your YouTube videos.");
    else status = el("p", { class: "ap-conn" }, "Not connected.");
    section.append(status);

    if (account) {
      section.append(details([
        ["Channel", account.name],
        ["Connected", when(account.connected_at)],
        ["Sign-in expires", account.token_expires_at ? when(account.token_expires_at) : null],
        ["Last published", account.last_publish_at ? when(account.last_publish_at) : "Nothing published yet"],
      ]));
    }

    section.append(el("h3", {}, "What Clip Maker asks Google for"),
                   permissionList(yt.permissions, p => el("span", { class: `ap-pill${p.for === "autopilot" ? " terra" : ""}` },
                                                         p.for === "autopilot" ? "Autopilot" : "Manual uploads")));

    if (!yt.client_configured) {
      if (yt.mode === "web") {
        section.append(callout("info", "info", "YouTube sign-in isn't set up on this server. Set ",
                               el("code", {}, "CLIPMAKER_GOOGLE_CLIENT_SECRET"), " and ",
                               el("code", {}, "CLIPMAKER_OAUTH_REDIRECT"), ", then restart Clip Maker."));
      } else {
        const file = el("input", { type: "file", accept: ".json,application/json", "data-focus": "yt-client" });
        const f = field("ap-setup-yt-client", "Google OAuth client (JSON file)", file,
                        { hint: "It's saved on this Mac as client_secret.json, readable by your account only." });
        file.addEventListener("change", () => addClient(f));
        section.append(el("h3", {}, "Add your Google OAuth client"),
                       el("ol", { class: "ap-steps" },
                          el("li", {}, "In ", link(GOOGLE_CONSOLE, "Google Cloud Console"),
                             ", create a project and enable the YouTube Data API v3."),
                          el("li", {}, "Create an OAuth client of type Desktop app and download its JSON file."),
                          el("li", {}, "Add the file here.")),
                       f.wrap);
      }
    }

    const acts = el("div", { class: "ap-card-acts" });
    if (yt.client_configured && !forAutopilot) {
      const label = lost ? "Reconnect for Autopilot" : yt.connected ? "Add the Autopilot permission"
                                                                    : "Connect for Autopilot";
      const connect = el("button", { type: "button", class: "ap-btn", "data-focus": "yt-connect" }, label);
      connect.addEventListener("click", () => connectYouTube(connect));
      acts.append(connect);
    } else if (!yt.client_configured) {
      acts.append(el("button", { type: "button", class: "ap-btn", disabled: true, "data-focus": "yt-connect",
                                 "aria-describedby": yt.mode === "web" ? null : "ap-setup-yt-client-hint" },
                     "Connect for Autopilot"));
    }
    if (yt.connected || account) {
      const off = el("button", { type: "button", class: "ap-btn ghost ap-danger", "data-focus": "yt-disconnect" },
                     "Disconnect YouTube");
      off.addEventListener("click", () => disconnect("youtube"));
      acts.append(off);
    }
    if (acts.childElementCount) section.append(acts);

    // The audience (required, 4.7) and the advisory declarations (11.1, 11.3).
    const audience = el("fieldset", { class: "ap-radios", "data-focus": "yt-audience", tabindex: "-1" },
                        el("legend", {}, "Channel audience"));
    const kids = account ? account.made_for_kids : null;
    for (const [value, label] of [[false, "Not made for kids"], [true, "Made for kids"]]) {
      const radio = el("input", { type: "radio", name: "ap-yt-audience", value: String(value), disabled: !account });
      radio.checked = kids === value;
      radio.addEventListener("change", () => declare({ made_for_kids: value }, `Channel audience: ${label.toLowerCase()}.`,
                                                     "yt-audience"));
      audience.append(el("label", { class: "ap-radio" }, radio, el("span", {}, label)));
    }
    audience.append(el("p", { class: "ap-hint-line" }, account
      ? "YouTube needs this for every upload, and Autopilot never guesses it."
      : "Connect YouTube first. YouTube needs this for every upload, and Autopilot never guesses it."));
    section.append(audience);

    const declarations = el("div", { class: "ap-declare" });
    for (const [key, focus, label, hint, extra] of [
      ["oauth_production_declared", "yt-oauth", "My Google Cloud app is published (In production)",
       "While it's in Testing, Google ends the sign-in every 7 days. Publish it in the OAuth consent screen.", null],
      ["audit_declared", "yt-audit", "My Google Cloud project passed YouTube's API audit",
       "Until then, videos uploaded through the API stay private. ",
       link("https://support.google.com/youtube/contact/yt_api_form", "YouTube's API audit form")],
    ]) {
      const box = el("input", { type: "checkbox", id: `ap-setup-${key}`, disabled: !account, "data-focus": focus,
                                "aria-describedby": `ap-setup-${key}-hint` });
      box.checked = !!(account && account[key]);
      box.addEventListener("change", () => declare({ [key]: box.checked },
                                                   `${label}: ${box.checked ? "yes" : "no"}.`, focus));
      declarations.append(el("div", { class: "ap-declare-row" },
                             el("label", { class: "ap-check-row", for: box.id }, box, el("span", {}, label)),
                             el("p", { class: "ap-hint-line", id: `ap-setup-${key}-hint` }, hint, extra)));
    }
    section.append(el("h3", {}, "Declarations"), declarations);

    const login = state.youtube.browser_login;
    if (login && login.connected && login.note) section.append(callout("info", "info", login.note));
    return section;
  }

  /* ── Instagram ────────────────────────────────────────────────────────── */
  function instagramSection() {
    const ig = state.instagram;
    const account = ig.account;
    const lost = account && account.status === "needs_reconnect";
    const section = el("section", { class: "ap-card", "aria-labelledby": "ap-setup-ig" },
                       el("h2", { id: "ap-setup-ig" }, "Instagram"));
    const result = ig.last_result;
    if (result && result.status === "error") {
      section.append(callout("err", "alert", el("b", {}, "Instagram wasn't connected. "), result.message));
    }
    if (lost) section.append(callout("err", "alert", account.status_reason || "Instagram access was lost. Reconnect to continue."));
    else if (ig.connected && account) {
      section.append(el("p", { class: "ap-conn ok" }, icon("check"), `Connected to @${account.name || "your account"} through Meta.`));
      for (const warning of (result && result.status === "connected" && result.warnings) || []) {
        section.append(callout("warn", "alert", warning));
      }
    } else if (!ig.choose) section.append(el("p", { class: "ap-conn" }, "Not connected."));

    if (ig.choose) section.append(picker(ig.choose));

    if (account) {
      const neverEnds = account.status === "ok" && !account.token_expires_at;
      section.append(details([
        ["Account", account.name ? `@${account.name}` : null],
        ["Connected", when(account.connected_at)],
        ["Sign-in expires", account.token_expires_at ? when(account.token_expires_at) : neverEnds ? "Doesn't expire" : null],
        ["Data access until", account.data_access_expires_at ? when(account.data_access_expires_at) : null],
        ["Last published", account.last_publish_at ? when(account.last_publish_at) : "Nothing published yet"],
      ]));
    }

    // The Meta app (9.2): saved here on this Mac, set on the server otherwise.
    section.append(el("h3", {}, "Your Meta app"));
    section.append(el("ol", { class: "ap-steps" }, ...ig.setup_steps.map(step => el("li", {}, step))),
                   el("p", { class: "ap-hint-line" }, link(META_APPS, "Open Meta for Developers")));
    section.append(el("h3", {}, "What Clip Maker asks Meta for"), el("ul", { class: "ap-perms" },
      ...ig.permissions.map(p => el("li", {}, el("code", {}, p.scope), el("span", {}, p.why)))));
    if (ig.mode === "web") {
      section.append(callout("info", "info", "On this server the Meta app is set with ",
                             el("code", {}, "CLIPMAKER_META_APP_ID"), ", ", el("code", {}, "CLIPMAKER_META_APP_SECRET"),
                             ", ", el("code", {}, "CLIPMAKER_META_CONFIG_ID"), " and ",
                             el("code", {}, "CLIPMAKER_META_REDIRECT"), "."));
    } else {
      section.append(appForm(ig));
    }

    const acts = el("div", { class: "ap-card-acts" });
    const connect = el("button", { type: "button", class: "ap-btn", "data-focus": "ig-connect",
                                   disabled: !ig.configured }, lost || ig.connected ? "Reconnect Instagram" : "Connect Instagram");
    connect.addEventListener("click", () => connectInstagram(connect));
    acts.append(connect);
    if (ig.connected || account) {
      const off = el("button", { type: "button", class: "ap-btn ghost ap-danger", "data-focus": "ig-disconnect" },
                     "Disconnect Instagram");
      off.addEventListener("click", () => disconnect("instagram"));
      acts.append(off);
    }
    section.append(acts);
    if (!ig.configured) {
      section.append(el("p", { class: "ap-hint-line" }, "Save your Meta app's settings first."));
    }
    section.append(pasteToken(ig));
    if (state.igLogin && state.igLogin.connected && ig.manual_login_note) {
      section.append(callout("info", "info", ig.manual_login_note));
    }
    return section;
  }

  function appForm(ig) {
    const app = ig.app || {};
    const id = field("ap-setup-ig-appid", "App ID", el("input", { type: "text", inputmode: "numeric", autocomplete: "off",
                                                                  value: app.app_id || "", "data-focus": "ig-appid" }));
    const secret = field("ap-setup-ig-secret", "App secret",
                         el("input", { type: "password", autocomplete: "off",
                                       placeholder: app.secret_set ? "Saved" : "" }),
                         { hint: "Saved on this Mac in meta_app.json, readable by your account only." +
                                 (app.secret_set ? " Enter it again to change these settings." : "") });
    const config = field("ap-setup-ig-config", "Login configuration ID",
                         el("input", { type: "text", autocomplete: "off", value: app.config_id || "" }),
                         { hint: "From Facebook Login for Business → Configurations." });
    const save = el("button", { type: "submit", class: "ap-btn ghost" }, "Save app settings");
    const form = el("form", { class: "ap-form", "aria-label": "Meta app settings", novalidate: true },
                    el("div", { class: "ap-form-grid" }, id.wrap, secret.wrap, config.wrap),
                    el("div", { class: "ap-card-acts" }, save));
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (state.busy) return;
      for (const f of [id, secret, config]) setError(f, "");
      const missing = [[id, "Enter the app ID."], [secret, "Enter the app secret."], [config, "Enter the configuration ID."]]
        .filter(([f]) => !f.control.value.trim());
      if (missing.length) {
        for (const [f, message] of missing) setError(f, message);
        missing[0][0].control.focus();
        return;
      }
      state.busy = true;
      try {
        await api.post("/api/connections/instagram/app", {
          app_id: id.control.value.trim(), app_secret: secret.control.value.trim(),
          config_id: config.control.value.trim() });
        say("Saved the Meta app settings.");
        await refresh("ig-connect");
      } catch (err) {
        setError(/app id/i.test(err.message) ? id : secret, err.message);
      } finally {
        state.busy = false;
      }
    });
    return form;
  }

  function picker(choose) {
    const box = el("fieldset", { class: "ap-radios ap-picker" },
                   el("legend", {}, "Choose the Instagram account Autopilot posts to"));
    choose.pages.forEach((page, i) => {
      const radio = el("input", { type: "radio", name: "ap-ig-page", value: page.page_id });
      radio.checked = i === 0;
      box.append(el("label", { class: "ap-radio" }, radio,
                    el("span", {}, el("b", {}, `@${page.ig_username || page.ig_name || page.ig_user_id}`),
                       ` · Facebook Page: ${page.page_name || page.page_id}`)));
    });
    const use = el("button", { type: "button", class: "ap-btn", "data-focus": "ig-pick" }, "Use this account");
    use.addEventListener("click", async () => {
      const chosen = box.querySelector("input:checked");
      if (!chosen || state.busy) return;
      state.busy = true;
      try {
        await api.post("/api/connections/instagram/select", { page_id: chosen.value });
        say("Instagram is connected.");
        document.dispatchEvent(new Event("ap:changed"));
        await refresh("ig-connect");
      } catch (err) {
        showProblem(err.message);
      } finally {
        state.busy = false;
      }
    });
    box.append(el("p", { class: "ap-hint-line" }, `Choose by ${when(choose.expires_at)}, or connect again.`),
               el("div", { class: "ap-card-acts" }, use));
    return box;
  }

  function pasteToken(ig) {
    const input = el("input", { type: "password", autocomplete: "off", spellcheck: "false" });
    const f = field("ap-setup-ig-token", "User access token", input,
                    { hint: "It's exchanged for a 60-day token and checked the same way as a sign-in." });
    const go = el("button", { type: "button", class: "ap-btn ghost", disabled: !ig.configured }, "Connect with this token");
    go.addEventListener("click", async () => {
      setError(f, "");
      const token = input.value.trim();
      if (!token) {
        setError(f, "Paste the token first.");
        input.focus();
        return;
      }
      if (state.busy) return;
      state.busy = true;
      go.setAttribute("aria-disabled", "true");
      try {
        const reply = await api.post("/api/connections/instagram/token", { token });
        say(reply.outcome === "choose" ? "Choose the Instagram account Autopilot posts to." : "Instagram is connected.");
        document.dispatchEvent(new Event("ap:changed"));
        await refresh(reply.outcome === "choose" ? "ig-pick" : "ig-connect");
      } catch (err) {
        setError(f, err.problems.length ? err.problems[0].message : err.message);
        input.focus();
      } finally {
        state.busy = false;
        go.removeAttribute("aria-disabled");
      }
    });
    return el("details", { class: "ap-paste" },
              el("summary", {}, "Sign-in can't come back to Clip Maker? Paste a token instead"),
              el("p", {}, "In Meta's ", link(GRAPH_EXPLORER, "Graph API Explorer"),
                 ", choose your app, generate a User Token with the permissions above, and paste it here."),
              f.wrap, el("div", { class: "ap-card-acts" }, go));
  }

  /* ── the time zone ────────────────────────────────────────────────────── */
  function timezoneSection() {
    const select = el("select", { "data-focus": "tz" });
    const list = zones();
    const current = state.zone;
    const all = current && !list.includes(current) ? [current, ...list] : list;
    if (!current) select.append(el("option", { value: "" }, "Not set"));
    for (const zone of all) select.append(el("option", { value: zone }, zone));
    select.value = current || "";
    const f = field("ap-setup-tz", "Time zone", select,
                    { hint: "Posting windows, and every time Autopilot shows, use this zone." });
    const save = el("button", { type: "button", class: "ap-btn ghost" }, "Save time zone");
    save.addEventListener("click", () => setZone(f, select.value));
    const mine = browserZone();
    const useMine = mine && mine !== current
      ? el("button", { type: "button", class: "ap-linkbtn" }, `Use this browser's zone, ${mine}`) : null;
    if (useMine) useMine.addEventListener("click", () => setZone(f, mine));
    const named = current ? el("p", { class: "ap-conn" }, `Times are in ${zoneLongName(current)} (${current}).`) : null;
    return el("section", { class: "ap-card", "aria-labelledby": "ap-setup-tz-h" },
              el("h2", { id: "ap-setup-tz-h" }, "Time zone"), named,
              el("div", { class: "ap-tz-row" }, f.wrap, save), useMine);
  }

  async function setZone(f, zone) {
    setError(f, "");
    if (!zone || state.busy) return;
    state.busy = true;
    try {
      await savePrefs({ timezone: zone });
      say(`Times are now in ${zone}.`);
      await refresh("tz");
    } catch (err) {
      setError(f, err.message);
    } finally {
      state.busy = false;
    }
  }

  /* ── actions ──────────────────────────────────────────────────────────── */
  async function addClient(f) {
    const file = f.control.files && f.control.files[0];
    setError(f, "");
    if (!file) return;
    try {
      await api.post("/api/youtube/client-secret", { content: await file.text() });
      say("Added your Google OAuth client.");
      await refresh("yt-connect");
    } catch (err) {
      setError(f, err.message);
    }
  }

  async function connectYouTube(button) {
    if (state.busy) return;
    if (state.youtube.mode === "web") {
      try {
        const { url } = await api.get("/api/connections/youtube/start");
        location.assign(url);
      } catch (err) {
        showProblem(err.message);
      }
      return;
    }
    state.busy = true;
    button.setAttribute("aria-disabled", "true");
    const wait = el("p", { class: "ap-conn", role: "status" }, el("span", { class: "spin", "aria-hidden": "true" }),
                    "Finish signing in in the browser window that opened. This page updates when you're done.");
    button.after(wait);
    try {
      await api.post("/api/connections/youtube/connect");
      say("YouTube is connected.");
      document.dispatchEvent(new Event("ap:changed"));
      await refresh("yt-audience");
    } catch (err) {
      wait.remove();
      showProblem(err.message);
    } finally {
      state.busy = false;
      button.removeAttribute("aria-disabled");
    }
  }

  async function connectInstagram(button) {
    if (state.busy || button.disabled) return;
    try {
      const { url } = await api.get("/api/connections/instagram/start");
      location.assign(url);                       // Meta returns to /?instagram=…
    } catch (err) {
      showProblem(err.message);
    }
  }

  async function declare(values, said, focus) {
    if (state.busy) return;
    state.busy = true;
    try {
      await api.post("/api/autopilot/youtube/declarations", values);
      say(said);
      document.dispatchEvent(new Event("ap:changed"));
      await refresh(focus);
    } catch (err) {
      showProblem(err.message);
      await refresh(focus);
    } finally {
      state.busy = false;
    }
  }

  async function disconnect(platform) {
    const name = platform === "youtube" ? "YouTube" : "Instagram";
    const content = platform === "youtube"
      ? [el("p", {}, "This deletes the stored sign-in and revokes it at Google. Posts not yet published wait " +
                     "until you reconnect; videos already scheduled on YouTube still go public on time."),
         el("p", {}, "Manual uploads use the same sign-in, so they stop working too.")]
      : [el("p", {}, "This deletes the stored Meta sign-in. Posts not yet published wait until you reconnect."),
         el("p", {}, "To remove the app's access at Meta as well, remove it under Facebook's Settings → " +
                     "Business integrations. The password login for manual uploads isn't affected.")];
    if (!await modal({ title: `Disconnect ${name}?`, content, confirm: `Disconnect ${name}`, danger: true })) return;
    try {
      const reply = await api.post(`/api/connections/${platform}/disconnect`);
      const waiting = reply.posts_waiting || 0;
      say(`Disconnected ${name}.${waiting ? ` ${plural(waiting, "post waits", "posts wait")} for a reconnect.` : ""}`);
      document.dispatchEvent(new Event("ap:changed"));
      await refresh(platform === "youtube" ? "yt-connect" : "ig-connect");
    } catch (err) {
      showProblem(err.message);
    }
  }

  /* ── rendering ────────────────────────────────────────────────────────── */
  function renderNotice() {
    notice.replaceChildren();
    const back = state.returned;
    if (!back) return;
    if (back.platform === "youtube") {
      notice.append(back.outcome === "connected"
        ? callout("ok", "check", "YouTube is connected.")
        : callout("err", "alert", "Google sign-in finished, but the channel couldn't be read. Connect again."));
    } else if (back.platform === "instagram") {
      const result = state.instagram && state.instagram.last_result;
      if (back.outcome === "connected") notice.append(callout("ok", "check", (result && result.message) || "Instagram is connected."));
      else if (back.outcome === "choose") notice.append(callout("info", "info", (result && result.message) ||
                                                                "Choose the Instagram account Autopilot posts to."));
    }
    state.returned = null;            // shown once
  }

  function renderAll(focus = null) {
    body.replaceChildren(checklist(), youtubeSection(), instagramSection(), timezoneSection());
    if (focus) {
      const target = body.querySelector(`[data-focus="${CSS.escape(focus)}"]`);
      if (target && !target.disabled) target.focus({ preventScroll: false });
      else heading.focus({ preventScroll: true });
    }
  }

  async function refresh(focus = null) {
    if (await load()) renderAll(focus);
  }

  if (await load()) {
    renderNotice();
    renderAll();
  }
}
