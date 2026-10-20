"use strict";

// ── дані й константи ────────────────────────────────────────────────────────
const STEPS = [
  { key: "prepare", label: "Підготовка" },
  { key: "review", label: "Перевірка" },
  { key: "publish", label: "Публікація" },
  { key: "telegram", label: "Telegram" },
];
const STAGE_STEP = { prepare: "prepare", review: "review", publish: "publish", telegram: "telegram", done: "telegram" };
const PREP_ITEMS = [
  ["audio", "Аудіофайл"],
  ["transcript", "Розшифровка мовлення"],
  ["games", "Пошук згаданих ігор"],
  ["metadata", "Заголовки й описи"],
  ["video", "Відео зі звуковою хвилею"],
  ["thumbnail", "Обкладинка для YouTube"],
];
const PRIVACY = {
  public: ["Публічне", "s-ok"],
  unlisted: ["Unlisted — лише за посиланням", "s-todo"],
  private: ["Приватне", "s-todo"],
};
const RECENT_JOB_MS = 2 * 60 * 1000; // скільки показувати журнал уже завершеного завдання

const app = {
  series: [],
  channel: "",
  episodes: [],
  nextNumber: "001",
  route: { name: "list" },
  detail: null,
  drafts: {}, // відредаговані описи до збереження: { [episode]: metadata }
  dirty: {},
  tg: {}, // відредагований текст поста: { [episode]: text }
  job: null,
  jobLines: [],
  hiddenJob: null,
  pollTimer: null,
};

// ── утиліти ─────────────────────────────────────────────────────────────────
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const enc = encodeURIComponent;
const getPath = (obj, path) => path.split(".").reduce((acc, k) => acc?.[k], obj);
const setPath = (obj, path, value) => {
  const keys = path.split(".");
  const last = keys.pop();
  keys.reduce((acc, k) => (acc[k] ??= {}), obj)[last] = value;
};
const clone = (v) => JSON.parse(JSON.stringify(v));
const pad = (n) => String(n).padStart(2, "0");

function parseLocal(s) {
  return s ? new Date(s.replace(" ", "T")) : null;
}
function fmtDate(s) {
  const d = parseLocal(s);
  if (!d || isNaN(d)) return s || "";
  return d.toLocaleString("uk-UA", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
}
function inputValue(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function tomorrowAt(hour) {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(hour, 0, 0, 0);
  return d;
}
function fmtBytes(n) {
  return n > 1e9 ? `${(n / 1e9).toFixed(2)} ГБ` : `${Math.round(n / 1e6)} МБ`;
}
function fmtDuration(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return h ? `${h}:${pad(m)}:${pad(sec % 60)}` : `${pad(m)}:${pad(sec % 60)}`;
}

async function api(method, path, body) {
  const opts = { method, headers: { "X-Podpub": "1" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new Error("Інтерфейс не відповідає — можливо, закрито вікно терміналу podpub. Запусти podpub знову.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Щось пішло не так (${res.status}).`);
  return data;
}

let toastTimer;
function toast(message, kind = "info") {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = "toast"), kind === "error" ? 8000 : 3500);
}

function ask({ title, body, ok = "Так", danger = false }) {
  const dlg = $("#dlg");
  $("#dlg-title").textContent = title;
  $("#dlg-body").innerHTML = body;
  const okBtn = $("#dlg-ok");
  okBtn.textContent = ok;
  okBtn.className = `btn ${danger ? "danger" : "primary"}`;
  dlg.returnValue = "";
  dlg.showModal();
  return new Promise((resolve) => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }));
}

function callout(tone, title, text, actions = "") {
  return `<div class="callout c-${tone}">
    <div class="callout-text"><b>${title}</b>${text ? `<p>${text}</p>` : ""}</div>
    ${actions ? `<div class="callout-actions">${actions}</div>` : ""}
  </div>`;
}

const isPublished = (d) => Boolean(d.published?.transistor_id || d.published?.youtube_id);
const needsGoPublic = (d) => ["unlisted", "private"].includes(d.published?.youtube_privacy);
const jobRunning = () => app.job?.status === "running";
const jobFor = (id, kinds) =>
  jobRunning() && app.job.episode === id && (!kinds || kinds.includes(app.job.kind));
const seriesChip = (e) =>
  e.series ? `<span class="chip s-${esc(e.series)}">${esc(e.series_name || e.series)}</span>` : "";
const epLabel = (e) => (e.number != null ? `#${e.number}` : e.id);

// ── завантаження даних і маршрутизація ──────────────────────────────────────
async function loadState() {
  const s = await api("GET", "/api/state");
  app.series = s.series;
  app.channel = s.channel;
  app.episodes = s.episodes;
  app.nextNumber = s.next_number;
}

async function loadDetail(id) {
  app.detail = await api("GET", `/api/episodes/${enc(id)}`);
  if (!app.drafts[id] && app.detail.metadata) app.drafts[id] = clone(app.detail.metadata);
}

function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "new") return { name: "new" };
  if (parts[0] === "e" && parts[1]) return { name: "episode", id: parts[1], step: parts[2] || null };
  return { name: "list" };
}

async function go() {
  app.route = parseRoute();
  try {
    await loadState();
    if (app.route.name === "episode") await loadDetail(app.route.id);
  } catch (e) {
    $("#main").innerHTML = `<a class="back" href="#/">← Усі випуски</a>${callout("warn", "Не вдалося відкрити", esc(e.message))}`;
    return;
  }
  render();
  window.scrollTo(0, 0);
}

async function refresh() {
  try {
    await loadState();
    if (app.route.name === "episode") await loadDetail(app.route.id);
  } catch (e) {
    toast(e.message, "error");
    return;
  }
  render();
}

// ── рендер ──────────────────────────────────────────────────────────────────
function render() {
  const main = $("#main");
  if (app.route.name === "new") {
    main.innerHTML = viewNew();
    document.title = "Новий випуск · podpub";
  } else if (app.route.name === "episode") {
    main.innerHTML = viewEpisode();
    document.title = `${epLabel(app.detail)} · podpub`;
  } else {
    main.innerHTML = viewList();
    document.title = "Випуски · podpub";
  }
  bindView();
  renderJob();
}

function stageInfo(e) {
  const p = e.published || {};
  if (jobFor(e.id)) return { text: "Виконується…", tone: "run" };
  switch (e.stage) {
    case "prepare":
      return e.busy ? { text: "Обробляється в терміналі", tone: "run" } : { text: "Підготовку не завершено", tone: "warn" };
    case "review":
      return { text: "Чекає перевірки", tone: "todo" };
    case "publish":
      return needsGoPublic(e) ? { text: "Відео ще не публічне", tone: "todo" } : { text: "Публікацію не завершено", tone: "warn" };
    case "telegram":
      return p.telegram_scheduled_at
        ? { text: `Пост заплановано · ${fmtDate(p.telegram_scheduled_at)}`, tone: "ok" }
        : { text: "Чекає поста в Telegram", tone: "todo" };
    default:
      if (p.youtube_privacy === "scheduled" && parseLocal(p.scheduled_at) > new Date())
        return { text: `Вийде ${fmtDate(p.scheduled_at)}`, tone: "ok" };
      return { text: "Опубліковано", tone: "ok" };
  }
}

function viewList() {
  const rows = app.episodes
    .map((e) => {
      const st = stageInfo(e);
      return `<li><a class="ep-row" href="#/e/${enc(e.id)}">
        <span class="ep-no">${esc(epLabel(e))}</span>
        <span class="ep-body">${seriesChip(e)}<span class="ep-name">${esc(e.title || "Назва з'явиться після підготовки")}</span></span>
        <span class="status s-${st.tone}">${esc(st.text)}</span>
      </a></li>`;
    })
    .join("");
  return `
    <div class="page-head">
      <div><p class="eyebrow">Конвеєр публікації</p><h1>Випуски</h1></div>
      <a class="btn primary big" href="#/new">Новий випуск</a>
    </div>
    ${
      rows
        ? `<ul class="ep-list">${rows}</ul>`
        : `<div class="empty"><p>Ще жодного випуску.</p><a class="btn primary" href="#/new">Підготувати перший</a></div>`
    }`;
}

function viewNew() {
  const busy = jobRunning();
  return `
    <a class="back" href="#/">← Усі випуски</a>
    <div class="page-head"><div><p class="eyebrow">Крок 1 з 4 · Підготовка</p><h1>Новий випуск</h1></div></div>
    <form id="new-form" class="block" novalidate>
      <fieldset class="field">
        <legend>Серія</legend>
        <div class="series-pick">${app.series
          .map(
            (s) => `<label class="series-card s-${esc(s.key)}">
              <input type="radio" name="series" value="${esc(s.key)}"><span class="series-name">${esc(s.name)}</span>
            </label>`
          )
          .join("")}</div>
      </fieldset>
      <label class="field">
        <span class="label">Номер випуску</span>
        <input class="input input-short" name="episode" value="${esc(app.nextNumber)}" inputmode="numeric" autocomplete="off">
      </label>
      <div class="field">
        <span class="label">Аудіофайл</span>
        <label class="drop" id="drop">
          <input type="file" name="audio" accept=".mp3,.wav,.m4a,.aac,.flac,.ogg,audio/*">
          <span class="drop-text" id="drop-text">Перетягни сюди аудіо або <u>обери файл</u></span>
        </label>
      </div>
      <p class="hint">Після завантаження почнеться підготовка: розшифровка, описи, відео. Для годинного випуску це 20–40 хвилин — нічого не публікується, доки ти сам не натиснеш «Опублікувати».</p>
      <div class="upload" id="upload" hidden><div class="bar"><span id="upload-bar"></span></div><span class="upload-text" id="upload-text"></span></div>
      <div class="form-actions">
        <button class="btn primary big" type="submit" ${busy ? "disabled" : ""}>Почати підготовку</button>
        ${busy ? `<span class="hint">Зачекай: зараз виконується «${esc(app.job.label)}».</span>` : ""}
      </div>
    </form>`;
}

function stepAllowed(d, key) {
  if (key === "prepare") return true;
  if (key === "review" || key === "publish") return d.prepared || isPublished(d);
  if (key === "telegram") return isPublished(d);
  return false;
}

function viewEpisode() {
  const d = app.detail;
  let step = app.route.step;
  if (!STEPS.some((s) => s.key === step) || !stepAllowed(d, step)) step = STAGE_STEP[d.stage];
  app.route.step = step;
  const panel = { prepare: panelPrepare, review: panelReview, publish: panelPublish, telegram: panelTelegram }[step];
  return `
    <a class="back" href="#/">← Усі випуски</a>
    <header class="ep-head">
      <div class="ep-meta">${seriesChip(d)}<span class="ep-no big">${esc(epLabel(d))}</span></div>
      <h1 class="ep-title-h">${esc(d.title || "Назва з'явиться після підготовки")}</h1>
    </header>
    ${stepper(d, step)}
    <div id="job-slot"></div>
    <section class="panel">${panel(d)}</section>`;
}

function stepper(d, current) {
  const p = d.published || {};
  const published = isPublished(d);
  const info = {
    prepare: d.prepared
      ? ["done", "Готово"]
      : jobFor(d.id, ["prepare"])
      ? ["run", "Триває…"]
      : d.busy
      ? ["run", "У терміналі"]
      : ["warn", "Не завершено"],
    review: !d.prepared && !published ? ["locked", "—"] : published ? ["done", "Перевірено"] : ["todo", "Чекає тебе"],
    publish: !d.prepared && !published
      ? ["locked", "—"]
      : !published
      ? ["todo", "Ще ні"]
      : !d.publish_complete
      ? ["warn", "Не завершено"]
      : needsGoPublic(d)
      ? ["todo", "Відео unlisted"]
      : ["done", p.youtube_privacy === "scheduled" ? "Заплановано" : "Готово"],
    telegram: !published
      ? ["locked", "—"]
      : d.stage === "done"
      ? ["done", p.telegram_sent_at ? "Надіслано" : "Раніше"]
      : p.telegram_scheduled_at
      ? ["todo", "Заплановано"]
      : d.telegram_draft
      ? ["todo", "Чернетка"]
      : ["todo", "Ще ні"],
  };
  const items = STEPS.map((s, i) => {
    const [tone, sub] = info[s.key];
    const cls = `step t-${tone}${s.key === current ? " is-current" : ""}`;
    const inner = `<span class="step-n">${tone === "done" ? "✓" : i + 1}</span>
      <span class="step-text"><span class="step-label">${s.label}</span><span class="step-sub">${esc(sub)}</span></span>`;
    return tone === "locked"
      ? `<li><span class="${cls}" aria-disabled="true">${inner}</span></li>`
      : `<li><a class="${cls}" href="#/e/${enc(d.id)}/${s.key}" ${s.key === current ? 'aria-current="step"' : ""}>${inner}</a></li>`;
  });
  return `<ol class="stepper">${items.join("")}</ol>`;
}

// ── крок 1: підготовка ──────────────────────────────────────────────────────
function panelPrepare(d) {
  const running = jobFor(d.id, ["prepare", "regenerate"]);
  const firstPending = PREP_ITEMS.find(([k]) => !d.steps[k])?.[0];
  const items = PREP_ITEMS.map(([key, label]) => {
    const state = d.steps[key] ? "done" : running && key === firstPending ? "run" : "todo";
    return `<li class="check ck-${state}"><span class="check-mark" aria-hidden="true"></span>${label}${
      state === "run" ? `<span class="check-note">виконується</span>` : ""
    }</li>`;
  }).join("");

  let foot;
  if (d.prepared) {
    foot = callout(
      "ok",
      "Підготовку завершено",
      "Тепер перевір заголовки, описи й відео — нічого не публікується без твого підтвердження.",
      `<a class="btn primary" href="#/e/${enc(d.id)}/review">Перевірити описи →</a>`
    );
  } else if (running) {
    foot = callout(
      "info",
      "Підготовка триває",
      "Можна закрити цю вкладку або займатися іншим — робота йде у фоні, поки відкрите вікно терміналу podpub. Mac сам не засне, але не закривай кришку ноутбука."
    );
  } else if (d.busy) {
    foot = callout(
      "info",
      "Обробляється в іншому вікні",
      "Цей випуск зараз обробляє команда в терміналі. Онови сторінку, коли вона завершиться.",
      `<button class="btn" data-act="refresh">Оновити</button>`
    );
  } else {
    foot = callout(
      "warn",
      "Підготовку не завершено",
      "Схоже, її перервано: закрилось вікно, Mac заснув або сталася помилка. Продовження почнеться з місця зупинки — зроблене повторно не виконується.",
      `<button class="btn primary" data-act="resume-prepare" ${jobRunning() ? "disabled" : ""}>Продовжити підготовку</button>`
    );
  }
  return `<h2 class="panel-title">Підготовка</h2><ul class="checks">${items}</ul>${foot}`;
}

// ── крок 2: перевірка ───────────────────────────────────────────────────────
function panelReview(d) {
  if (!d.metadata) return callout("warn", "Описів ще немає", "Спершу заверши підготовку.");
  const locked = isPublished(d);
  const busy = jobFor(d.id);
  const draft = app.drafts[d.id] || (app.drafts[d.id] = clone(d.metadata));
  const off = locked || busy ? "disabled" : "";
  const v = Date.now();

  const input = (path, label, { list = false, max = 0, hint = "" } = {}) => {
    let value = getPath(draft, path);
    if (list) value = (value || []).join(", ");
    return `<label class="field">
      <span class="label">${label}${hint ? ` <span class="hint-inline">${hint}</span>` : ""}${max ? `<span class="count" data-count="${path}" data-max="${max}"></span>` : ""}</span>
      <input class="input" data-field="${path}" ${list ? "data-list" : ""} value="${esc(value)}" ${off}>
    </label>`;
  };
  const textarea = (path, label, rows, max = 0) => `<label class="field">
      <span class="label">${label}${max ? `<span class="count" data-count="${path}" data-max="${max}"></span>` : ""}</span>
      <textarea class="input prose" data-field="${path}" rows="${rows}" ${off}>${esc(getPath(draft, path))}</textarea>
    </label>`;

  return `
    <h2 class="panel-title">Перевірка</h2>
    ${
      locked
        ? callout("info", "Випуск уже опубліковано", "Нижче — описи в тому вигляді, як вони пішли на платформи. Редагувати їх тут уже немає сенсу.")
        : ""
    }
    <div class="media">
      <figure><video controls preload="metadata" src="/media/${enc(d.id)}/video.mp4?v=${d.media_version}"></video>
        <figcaption>Відео для YouTube — перемотай у кілька місць і перевір звук.</figcaption></figure>
      <figure><img src="/media/${enc(d.id)}/thumbnail.jpg?v=${v}" alt="Обкладинка випуску для YouTube">
        <figcaption>Обкладинка для YouTube</figcaption></figure>
    </div>
    ${gamesBlock(d, locked || busy)}
    <section class="block">
      <h3 class="block-title">Подкаст <small>Transistor → Spotify, Apple, Amazon, Deezer</small></h3>
      ${input("podcast.title", "Заголовок")}
      ${input("podcast.summary", "Короткий анонс", { hint: "1–2 речення" })}
      ${textarea("podcast.description", "Опис", 16)}
      ${input("podcast.keywords", "Ключові слова", { list: true, hint: "через кому" })}
    </section>
    <section class="block">
      <h3 class="block-title">YouTube</h3>
      ${input("youtube.title", "Заголовок", { max: 100 })}
      ${textarea("youtube.description", "Опис", 20, 5000)}
      ${input("youtube.tags", "Теги", { list: true, hint: "через кому" })}
    </section>
    ${
      locked
        ? ""
        : `<div class="actionbar">
      <span class="save-state" id="save-state"></span>
      <button class="btn ghost" data-act="regen" ${off}>Переписати описи заново</button>
      <button class="btn" data-act="save" ${off}>Зберегти</button>
      <button class="btn primary" data-act="to-publish" ${off}>Далі: публікація →</button>
    </div>`
    }`;
}

function gamesBlock(d, locked) {
  const g = d.games || { matched: [], uncertain: [] };
  const matched = (g.matched || []).map((n) => `<span class="chip game">${esc(n)}</span>`).join("");
  const uncertain = (g.uncertain || [])
    .map(
      (n) => `<li class="unc" data-game="${esc(n)}">
        <span class="chip game warn">${esc(n)}</span>
        ${locked ? "" : `<button class="link" data-act="fix-game">Виправити назву</button><button class="link muted" data-act="dismiss-game">Це не гра</button>`}
      </li>`
    )
    .join("");
  return `<section class="block">
    <h3 class="block-title">Згадані ігри</h3>
    <div class="chips">${matched || `<span class="muted">Жодної гри не знайдено.</span>`}</div>
    ${
      uncertain
        ? `<p class="sub">Варто перевірити — назву могли неправильно почути при розшифровці:</p>
           <ul class="unc-list">${uncertain}</ul>
           <p class="hint">Виправлена назва запам'ятовується назавжди — наступного разу її впізнає сама система.</p>`
        : `<p class="hint">Сумнівних назв немає.</p>`
    }
    ${
      d.metadata_stale && !locked
        ? callout(
            "warn",
            "Описи ще зі старими назвами",
            "Список ігор змінився. Онови описи, щоб виправлені назви потрапили в тексти.",
            `<button class="btn" data-act="regen">Оновити описи</button>`
          )
        : ""
    }
  </section>`;
}

// ── крок 3: публікація ──────────────────────────────────────────────────────
function panelPublish(d) {
  const p = d.published || {};
  const busy = jobFor(d.id);
  if (!isPublished(d)) {
    const def = inputValue(tomorrowAt(9));
    return `
      <h2 class="panel-title">Публікація</h2>
      <section class="block">
        <h3 class="block-title">Коли випускаємо?</h3>
        <div class="options">
          <label class="option"><input type="radio" name="when" value="now" checked>
            <b>Зараз</b>
            <p>Епізод одразу вийде на Transistor, а звідти — у Spotify, Apple Podcasts, Amazon і Deezer. Відео на YouTube завантажиться як <i>unlisted</i>: переглянеш його й відкриєш для всіх одним кліком.</p>
          </label>
          <label class="option"><input type="radio" name="when" value="later">
            <b>Запланувати</b>
            <p>Transistor і YouTube самі опублікують усе у вибраний час — Mac для цього вмикати не треба.</p>
            <input class="input when" type="datetime-local" id="publish-at" value="${def}" min="${inputValue(new Date())}" disabled>
          </label>
        </div>
        <button class="btn primary big" data-act="publish" ${busy || jobRunning() ? "disabled" : ""}>Опублікувати випуск</button>
      </section>
      <p class="hint">Перед публікацією ще раз глянь «Перевірку» — після виходу змінити описи вже можна лише вручну на платформах.</p>`;
  }

  const tStatus = p.transistor_status === "scheduled"
    ? [`Вийде ${fmtDate(p.scheduled_at)}`, "s-ok"]
    : p.transistor_status === "published" || (d.legacy && p.transistor_url)
    ? ["Вийшов", "s-ok"]
    : p.transistor_id
    ? ["Завантажено, ще не опубліковано", "s-warn"]
    : ["Ще ні", "s-warn"];
  const [yText, yTone] = !p.youtube_id
    ? ["Ще не завантажено", "s-warn"]
    : p.youtube_privacy === "scheduled"
    ? [`Відкриється ${fmtDate(p.scheduled_at)}`, "s-ok"]
    : PRIVACY[p.youtube_privacy] || ["Опубліковано раніше", "s-ok"];
  const sub = (done, label) => `<span class="${done ? "ok" : "no"}">${label}</span>`;
  const youtubeSubsteps = d.legacy
    ? ""
    : `<div class="substeps">
        ${sub(p.youtube_thumbnail_set, "Обкладинка")}
        ${d.playlist ? sub(p.youtube_playlist, `Плейлист «${esc(d.playlist)}»`) : ""}
        ${sub(p.transistor_video_url_set, "Посилання на відео в Transistor")}
      </div>`;

  let attention = "";
  if (!d.publish_complete) {
    attention = callout(
      "warn",
      "Публікацію не завершено",
      "Частина кроків не встигла виконатися — наприклад, через тимчасовий збій YouTube. Продовження нічого не задублює: зроблене буде пропущено.",
      `<button class="btn primary" data-act="continue-publish" ${jobRunning() ? "disabled" : ""}>Продовжити публікацію</button>`
    );
  } else if (needsGoPublic(d)) {
    attention = callout(
      "todo",
      "Відео поки бачать лише за посиланням",
      "Переглянь його на YouTube — і, якщо все гаразд, відкрий для всіх.",
      `<a class="btn" href="${esc(p.youtube_url)}" target="_blank" rel="noopener">Переглянути ↗</a>
       <button class="btn primary" data-act="go-public" ${jobRunning() ? "disabled" : ""}>Зробити публічним</button>`
    );
  } else {
    attention = callout(
      "ok",
      p.youtube_privacy === "scheduled" ? "Усе заплановано" : "Усе опубліковано",
      p.youtube_privacy === "scheduled"
        ? `Transistor і YouTube випустять епізод ${fmtDate(p.scheduled_at)}. Пост у Telegram можна підготувати вже зараз.`
        : "",
      `<a class="btn primary" href="#/e/${enc(d.id)}/telegram">Далі: пост у Telegram →</a>`
    );
  }

  return `
    <h2 class="panel-title">Публікація</h2>
    <section class="block">
      <dl class="platforms">
        <div class="platform"><dt>Transistor</dt><dd>
          <span class="status ${tStatus[1]}">${esc(tStatus[0])}</span>
          ${p.transistor_url ? `<a class="ext" href="${esc(p.transistor_url)}" target="_blank" rel="noopener">Відкрити ↗</a>` : ""}
        </dd></div>
        <div class="platform"><dt>YouTube</dt><dd>
          <span class="status ${yTone}">${esc(yText)}</span>
          ${p.youtube_url ? `<a class="ext" href="${esc(p.youtube_url)}" target="_blank" rel="noopener">Відкрити ↗</a>` : ""}
          ${youtubeSubsteps}
        </dd></div>
      </dl>
    </section>
    ${attention}
    ${
      d.legacy && !p.youtube_privacy
        ? `<p class="hint">Цей випуск опубліковано до появи інтерфейсу, тож його статус на YouTube тут невідомий. Якщо відео ще unlisted —
           <button class="link" data-act="go-public">зробити його публічним</button>.</p>`
        : ""
    }
    <p class="hint">Проблеми з входом у YouTube? <button class="link" data-act="yt-login" ${jobRunning() ? "disabled" : ""}>Увійти заново</button> — відкриється вікно Google.</p>`;
}

// ── крок 4: Telegram ────────────────────────────────────────────────────────
function tgPreview(text) {
  let h = esc(text);
  h = h.replace(/&lt;(\/?)(b|strong|i|em|u|s|code|blockquote)&gt;/g, "<$1$2>");
  h = h.replace(/&lt;a href=&quot;(https?:\/\/.*?)&quot;&gt;/g, '<a href="$1" target="_blank" rel="noopener">');
  h = h.replace(/&lt;\/a&gt;/g, "</a>");
  return h;
}
const tgLength = (text) => text.replace(/<[^>]+>/g, "").length;

function panelTelegram(d) {
  const p = d.published || {};
  const busy = jobFor(d.id) || jobRunning();
  const head = `<h2 class="panel-title">Пост у Telegram</h2>`;

  if (!d.telegram_draft) {
    const releaseNote =
      p.youtube_privacy === "scheduled" && parseLocal(p.scheduled_at) > new Date()
        ? ` Епізод вийде ${fmtDate(p.scheduled_at)} — пост можна підготувати зараз і запланувати на після виходу.`
        : "";
    return `${head}${callout(
      "todo",
      "Чернетки ще немає",
      `Claude напише анонс у стилі каналу ${esc(app.channel)}: заголовок, жарт, TL;DR, посилання і згадані ігри.${releaseNote}`,
      `<button class="btn primary" data-act="tg-draft" ${busy ? "disabled" : ""}>Скласти чернетку</button>`
    )}`;
  }

  const sent = p.telegram_sent_at;
  const text = app.tg[d.id] ?? d.telegram ?? "";
  const legacyUnknown = d.legacy && !sent;
  const off = sent || busy ? "disabled" : "";

  let sendBlock;
  if (sent) {
    sendBlock = callout("ok", `Надіслано ${fmtDate(sent)}`, `Пост уже в каналі ${esc(app.channel)}.`);
  } else if (p.telegram_scheduled_at) {
    sendBlock = callout(
      "info",
      `Заплановано на ${fmtDate(p.telegram_scheduled_at)}`,
      "Пост відправить цей Mac. Якщо в цей час він спатиме — пост піде, щойно Mac прокинеться (протягом доби). Якщо Mac буде вимкнений або ти вийдеш з облікового запису — пост не надішлеться.",
      `<button class="btn" data-act="tg-cancel">Скасувати планування</button>
       <button class="btn primary" data-act="tg-send-now" ${busy ? "disabled" : ""}>Надіслати зараз</button>`
    );
  } else {
    const release = p.youtube_privacy === "scheduled" ? parseLocal(p.scheduled_at) : null;
    const def = release && release > new Date() ? new Date(release.getTime() + 15 * 60000) : tomorrowAt(10);
    sendBlock = `<section class="block">
      <h3 class="block-title">Відправка в ${esc(app.channel)}</h3>
      <div class="options">
        <label class="option"><input type="radio" name="tg-when" value="now" ${release && release > new Date() ? "disabled" : "checked"}>
          <b>Надіслати зараз</b>
          <p>${release && release > new Date() ? "Недоступно: епізод ще не вийшов." : "Пост одразу з'явиться в каналі."}</p>
        </label>
        <label class="option"><input type="radio" name="tg-when" value="later" ${release && release > new Date() ? "checked" : ""}>
          <b>Запланувати</b>
          <p>Пост відправить цей Mac у вибраний час. Якщо він спатиме — пост піде, щойно прокинеться; вимкнений Mac пост не надішле.</p>
          <input class="input when" type="datetime-local" id="tg-at" value="${inputValue(def)}" min="${inputValue(new Date())}">
        </label>
      </div>
      <button class="btn primary big" data-act="tg-send" ${busy ? "disabled" : ""}>Надіслати</button>
    </section>`;
  }

  return `${head}
    ${
      legacyUnknown
        ? callout("warn", "Невідомо, чи пост уже виходив", "Цей випуск опубліковано до появи інтерфейсу. Перш ніж надсилати, перевір канал — щоб пост не вийшов двічі.")
        : ""
    }
    <section class="block">
      <div class="tg-grid">
        <label class="field">
          <span class="label">Текст поста <span class="hint-inline">HTML-теги &lt;b&gt;, &lt;i&gt;, &lt;a&gt;, &lt;blockquote&gt;</span></span>
          <textarea class="input code" id="tg-text" rows="24" ${off}>${esc(text)}</textarea>
        </label>
        <div>
          <span class="label">Як це виглядатиме</span>
          <div class="tg-preview-wrap">
            <div class="tg-bubble" id="tg-preview">${tgPreview(text)}</div>
            <div class="tg-meta"><span>Превʼю посилання: відео на YouTube</span><span id="tg-count"></span></div>
          </div>
        </div>
      </div>
      ${
        sent
          ? ""
          : `<div class="form-actions">
        <button class="btn" data-act="tg-save" ${off}>Зберегти текст</button>
        <button class="link muted" data-act="tg-regen" ${off}>Переписати пост заново</button>
        <span class="save-state" id="tg-state"></span>
      </div>`
      }
    </section>
    ${sendBlock}`;
}

// ── прив'язка подій після рендеру ──────────────────────────────────────────
function bindView() {
  if (app.route.name === "new") bindNew();
  if (app.route.name !== "episode") return;
  const d = app.detail;

  $$("[data-field]").forEach((el) =>
    el.addEventListener("input", () => {
      const draft = app.drafts[d.id];
      const value = el.hasAttribute("data-list")
        ? el.value.split(",").map((s) => s.trim()).filter(Boolean)
        : el.value;
      setPath(draft, el.dataset.field, value);
      app.dirty[d.id] = true;
      updateCounts();
      updateSaveState();
    })
  );
  updateCounts();
  updateSaveState();

  const tgSend = $('[data-act="tg-send"]');
  const tgWhen = $$('input[name="tg-when"]');
  const syncTgButton = () => {
    if (tgSend) tgSend.textContent = $('input[name="tg-when"]:checked')?.value === "later" ? "Запланувати пост" : "Надіслати зараз";
  };
  tgWhen.forEach((r) => r.addEventListener("change", syncTgButton));
  syncTgButton();

  const when = $$('input[name="when"]');
  when.forEach((r) => r.addEventListener("change", () => ($("#publish-at").disabled = r.value !== "later" || !r.checked)));

  const tg = $("#tg-text");
  if (tg) {
    const update = () => {
      $("#tg-preview").innerHTML = tgPreview(tg.value);
      const n = tgLength(tg.value);
      const count = $("#tg-count");
      count.textContent = `${n} / 4096`;
      count.style.color = n > 4096 ? "var(--accent)" : "";
    };
    tg.addEventListener("input", () => {
      app.tg[d.id] = tg.value;
      const state = $("#tg-state");
      if (state) {
        state.textContent = "Є незбережені зміни";
        state.classList.add("dirty");
      }
      update();
    });
    update();
  }
}

function updateCounts() {
  $$("[data-count]").forEach((el) => {
    const value = getPath(app.drafts[app.detail.id], el.dataset.count) || "";
    const max = Number(el.dataset.max);
    el.textContent = `${value.length} / ${max}`;
    el.classList.toggle("over", value.length > max);
  });
}

function updateSaveState() {
  const el = $("#save-state");
  if (!el) return;
  const dirty = app.dirty[app.detail.id];
  el.textContent = dirty ? "Є незбережені зміни" : "Усі зміни збережено";
  el.classList.toggle("dirty", Boolean(dirty));
}

function bindNew() {
  const form = $("#new-form");
  const file = $('input[name="audio"]', form);
  const drop = $("#drop");
  const show = () => {
    const f = file.files[0];
    drop.classList.toggle("has-file", Boolean(f));
    $("#drop-text").innerHTML = f
      ? `<span class="drop-file">${esc(f.name)}</span><span class="hint">${fmtBytes(f.size)} · клацни, щоб замінити</span>`
      : `Перетягни сюди аудіо або <u>обери файл</u>`;
  };
  file.addEventListener("change", show);
  ["dragenter", "dragover"].forEach((t) =>
    drop.addEventListener(t, (e) => {
      e.preventDefault();
      drop.classList.add("over");
    })
  );
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    if (e.dataTransfer.files.length) {
      file.files = e.dataTransfer.files;
      show();
    }
  });
  form.addEventListener("submit", submitNew);
}

function submitNew(e) {
  e.preventDefault();
  const form = e.target;
  const series = $('input[name="series"]:checked', form)?.value;
  const episode = form.episode.value.trim();
  const audio = form.audio.files[0];
  if (!series) return toast("Обери серію — від неї залежать обкладинка, назва й плейлист.", "error");
  if (!/^[0-9A-Za-z_-]{1,32}$/.test(episode)) return toast("Номер випуску — лише цифри (наприклад, 027).", "error");
  if (!audio) return toast("Додай аудіофайл випуску.", "error");

  const button = $('button[type="submit"]', form);
  button.disabled = true;
  $("#upload").hidden = false;
  const data = new FormData();
  data.append("episode", episode);
  data.append("series", series);
  data.append("audio", audio);

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/episodes");
  xhr.setRequestHeader("X-Podpub", "1");
  xhr.upload.onprogress = (ev) => {
    if (!ev.lengthComputable) return;
    const pct = Math.round((ev.loaded / ev.total) * 100);
    $("#upload-bar").style.width = `${pct}%`;
    $("#upload-text").textContent = pct < 100 ? `Завантаження ${pct}%` : "Запускаю підготовку…";
  };
  xhr.onload = () => {
    let body = {};
    try {
      body = JSON.parse(xhr.responseText);
    } catch {}
    if (xhr.status >= 200 && xhr.status < 300) {
      app.hiddenJob = null;
      app.expectJob = true;
      location.hash = `#/e/${enc(body.episode)}/prepare`;
      pollJob();
    } else {
      button.disabled = false;
      $("#upload").hidden = true;
      toast(typeof body.detail === "string" ? body.detail : "Не вдалося завантажити файл.", "error");
    }
  };
  xhr.onerror = () => {
    button.disabled = false;
    $("#upload").hidden = true;
    toast("Інтерфейс не відповідає — можливо, закрито вікно терміналу podpub.", "error");
  };
  xhr.send(data);
}

// ── дії ─────────────────────────────────────────────────────────────────────
const epPath = (suffix = "") => `/api/episodes/${enc(app.detail.id)}${suffix}`;

async function startJob(method, path, body) {
  await api(method, path, body);
  app.hiddenJob = null;
  app.expectJob = true;
  await pollJob();
  render();
}

async function saveMetadata() {
  const d = app.detail;
  await api("PUT", epPath("/metadata"), app.drafts[d.id]);
  app.dirty[d.id] = false;
  updateSaveState();
}

async function saveTelegram() {
  const d = app.detail;
  if (app.tg[d.id] === undefined) return;
  await api("PUT", epPath("/telegram"), { text: app.tg[d.id] });
  delete app.tg[d.id];
  const state = $("#tg-state");
  if (state) {
    state.textContent = "Збережено";
    state.classList.remove("dirty");
  }
}

async function sendTelegramNow() {
  const legacy = app.detail.legacy && !app.detail.published?.telegram_sent_at;
  const ok = await ask({
    title: "Надіслати пост у канал?",
    body: `<p>Пост одразу з'явиться в ${esc(app.channel)}. Прибрати його можна буде лише вручну в Telegram.</p>${
      legacy ? "<p><b>Переконайся, що пост цього випуску ще не виходив.</b></p>" : ""
    }`,
    ok: "Надіслати",
  });
  if (!ok) return;
  await saveTelegram();
  await startJob("POST", epPath("/telegram/send"));
}

const ACTIONS = {
  refresh: () => refresh(),
  "resume-prepare": () => startJob("POST", epPath("/prepare")),

  save: async () => {
    await saveMetadata();
    toast("Зміни збережено", "ok");
    await refresh();
  },
  regen: async () => {
    const ok = await ask({
      title: "Переписати описи заново?",
      body: "<p>Claude заново напише заголовки й описи з урахуванням поточного списку ігор. Твої ручні правки в описах зникнуть.</p>",
      ok: "Переписати",
    });
    if (!ok) return;
    delete app.drafts[app.detail.id];
    app.dirty[app.detail.id] = false;
    await startJob("POST", epPath("/regenerate-metadata"));
  },
  "to-publish": async () => {
    if (app.dirty[app.detail.id]) await saveMetadata();
    location.hash = `#/e/${enc(app.detail.id)}/publish`;
  },
  "fix-game": (btn) => {
    const li = btn.closest(".unc");
    const name = li.dataset.game;
    li.innerHTML = `<span class="chip game warn">${esc(name)}</span>
      <form class="fix-form"><input class="input" name="canonical" placeholder="Правильна назва гри" value="${esc(name.replace(/\?$/, ""))}" required>
      <button class="btn small primary">Зберегти</button><button type="button" class="link muted" data-act="refresh">Скасувати</button></form>`;
    const field = $("input", li);
    field.focus();
    field.select();
  },
  "dismiss-game": async (btn) => {
    await api("POST", epPath("/games/dismiss"), { original: btn.closest(".unc").dataset.game });
    await refresh();
  },

  publish: async () => {
    const later = $('input[name="when"]:checked')?.value === "later";
    const at = $("#publish-at")?.value;
    if (later && !at) return toast("Обери дату й час публікації.", "error");
    const label = epLabel(app.detail);
    const ok = await ask({
      title: later ? "Запланувати випуск?" : "Опублікувати випуск?",
      body: later
        ? `<p>Епізод ${esc(label)} автоматично вийде на Transistor і YouTube <b>${esc(fmtDate(at.replace("T", " ")))}</b>.</p>`
        : `<p>Епізод ${esc(label)} одразу вийде на Transistor, а звідти — у Spotify, Apple, Amazon і Deezer. Скасувати вихід уже не вийде.</p>
           <p>Відео на YouTube буде <i>unlisted</i>, поки ти його не відкриєш.</p>`,
      ok: later ? "Запланувати" : "Опублікувати",
    });
    if (!ok) return;
    if (app.dirty[app.detail.id]) await saveMetadata();
    await startJob("POST", epPath("/publish"), { schedule: later ? at : "" });
  },
  "continue-publish": () => startJob("POST", epPath("/publish"), { schedule: "" }),
  "go-public": async () => {
    const ok = await ask({
      title: "Відкрити відео для всіх?",
      body: "<p>Відео стане публічним на YouTube — з'явиться на каналі, в пошуку й рекомендаціях.</p>",
      ok: "Зробити публічним",
    });
    if (ok) await startJob("POST", epPath("/youtube-public"));
  },
  "yt-login": () => startJob("POST", "/api/youtube/login"),

  "tg-draft": () => startJob("POST", epPath("/telegram/draft"), { regenerate: false }),
  "tg-regen": async () => {
    const ok = await ask({
      title: "Переписати пост заново?",
      body: "<p>Claude напише новий варіант поста. Поточний текст, разом із твоїми правками, зникне.</p>",
      ok: "Переписати",
    });
    if (!ok) return;
    delete app.tg[app.detail.id];
    await startJob("POST", epPath("/telegram/draft"), { regenerate: true });
  },
  "tg-save": async () => {
    await saveTelegram();
    toast("Текст поста збережено", "ok");
  },
  "tg-send": async () => {
    const later = $('input[name="tg-when"]:checked')?.value === "later";
    if (!later) return sendTelegramNow();
    const at = $("#tg-at")?.value;
    if (!at) return toast("Обери дату й час відправки.", "error");
    await saveTelegram();
    await api("POST", epPath("/telegram/schedule"), { at });
    toast(`Пост заплановано на ${fmtDate(at.replace("T", " "))}`, "ok");
    await refresh();
  },
  "tg-send-now": () => sendTelegramNow(),
  "tg-cancel": async () => {
    await api("DELETE", epPath("/telegram/schedule"));
    toast("Планування скасовано", "ok");
    await refresh();
  },

  "hide-job": () => {
    app.hiddenJob = app.job?.id;
    renderJob();
  },
  retry: () => {
    const j = app.job;
    const base = `/api/episodes/${enc(j.episode || "")}`;
    const map = {
      prepare: ["POST", `${base}/prepare`],
      regenerate: ["POST", `${base}/regenerate-metadata`],
      publish: ["POST", `${base}/publish`, { schedule: "" }],
      "youtube-public": ["POST", `${base}/youtube-public`],
      "youtube-login": ["POST", "/api/youtube/login"],
      "telegram-draft": ["POST", `${base}/telegram/draft`, { regenerate: false }],
      "telegram-send": ["POST", `${base}/telegram/send`],
    };
    const [method, path, body] = map[j.kind];
    return startJob(method, path, body);
  },
};

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-act]");
  if (!btn || btn.disabled || btn.closest("dialog")) return;
  const action = ACTIONS[btn.dataset.act];
  if (!action) return;
  ev.preventDefault();
  btn.disabled = true;
  try {
    await action(btn);
  } catch (e) {
    toast(e.message, "error");
  } finally {
    if (btn.isConnected) btn.disabled = false;
  }
});

document.addEventListener("submit", async (ev) => {
  if (!ev.target.classList.contains("fix-form")) return;
  ev.preventDefault();
  const form = ev.target;
  const original = form.closest(".unc").dataset.game;
  const canonical = form.canonical.value.trim();
  if (!canonical) return;
  try {
    await api("POST", epPath("/games/resolve"), { original, canonical });
    toast(`«${canonical}» додано в довідник`, "ok");
    await refresh();
  } catch (e) {
    toast(e.message, "error");
  }
});

window.addEventListener("beforeunload", (e) => {
  if (Object.values(app.dirty).some(Boolean) || Object.keys(app.tg).length) e.preventDefault();
});

// ── фонові завдання ─────────────────────────────────────────────────────────
function jobVisible(j) {
  if (!j || j.id === app.hiddenJob) return false;
  if (app.route.name !== "episode") return false;
  if (j.episode && j.episode !== app.detail?.id) return false;
  if (j.status === "running" || j.status === "error") return true;
  return Date.now() - j.finished * 1000 < RECENT_JOB_MS;
}

function renderJob() {
  const j = app.job;
  $("#job-pill").innerHTML =
    j && j.status === "running"
      ? `<a class="job-pill" href="${j.episode ? `#/e/${enc(j.episode)}` : "#/"}"><span class="spinner"></span>${esc(j.label)}<span class="job-time" data-since="${j.started}"></span></a>`
      : "";
  const slot = $("#job-slot");
  if (slot) {
    if (!jobVisible(j)) {
      slot.innerHTML = "";
    } else {
      const lines = app.jobLines.map((l) => `<div class="ln k-${esc(l.kind)}">${esc(l.text)}</div>`).join("");
      const mark =
        j.status === "running" ? `<span class="spinner"></span>` : `<span class="dot ${j.status === "done" ? "ok" : "err"}"></span>`;
      const state = { running: "виконується", done: "готово", error: "зупинилося з помилкою" }[j.status];
      slot.innerHTML = `<div class="job">
        <div class="job-head">${mark}<b>${esc(j.label)}</b><span class="job-state">${state}</span>
          <span class="job-time" data-since="${j.started}" data-until="${j.finished || ""}"></span>
          ${j.status !== "running" ? `<button class="link muted" data-act="hide-job">Сховати</button>` : ""}
        </div>
        <div class="console" role="log" aria-live="polite">${lines || `<div class="ln k-info">Починаю…</div>`}</div>
        ${
          j.status === "error"
            ? `<div class="job-error"><p>${esc(j.error)}</p><button class="btn primary" data-act="retry">Спробувати ще раз</button></div>`
            : ""
        }
      </div>`;
      const consoleEl = $(".console", slot);
      consoleEl.scrollTop = consoleEl.scrollHeight;
    }
  }
  tickTimes();
}

function tickTimes() {
  $$(".job-time[data-since]").forEach((el) => {
    const until = el.dataset.until ? Number(el.dataset.until) : Date.now() / 1000;
    el.textContent = fmtDuration(until - Number(el.dataset.since));
  });
}
setInterval(tickTimes, 1000);

async function pollJob() {
  clearTimeout(app.pollTimer);
  const known = app.job?.id || "";
  let data;
  try {
    data = await api("GET", `/api/job?since=${known ? app.jobLines.length : 0}&id=${known}`);
  } catch {
    app.pollTimer = setTimeout(pollJob, 5000);
    return;
  }
  const j = data.job;
  if (!j) {
    app.job = null;
    renderJob();
    return;
  }
  if (j.id !== known) app.jobLines = [];
  app.jobLines.push(...j.lines);
  const progressed = j.status === "running" && j.lines.some((l) => l.kind === "ok");
  // expectJob: завдання щойно запущене з цієї сторінки — навіть якщо воно встигло
  // завершитися до першого опитування, сторінку все одно треба оновити.
  const wasRunning = (app.job?.id === j.id && app.job.status === "running") || app.expectJob;
  app.expectJob = false;
  app.job = j;
  renderJob();
  if (j.status === "running") {
    // Завершився черговий крок підготовки — оновлюємо чекліст (лише на кроці «Підготовка»,
    // щоб не перемальовувати сторінку, де людина може щось редагувати).
    if (progressed && app.route.name === "episode" && app.route.id === j.episode && app.route.step === "prepare") {
      await loadDetail(j.episode).catch(() => {});
      render();
    }
    app.pollTimer = setTimeout(pollJob, 1200);
  } else if (wasRunning) {
    toast(j.status === "done" ? `${j.label}: готово` : `${j.label}: помилка`, j.status === "done" ? "ok" : "error");
    if (j.episode && ["prepare", "regenerate"].includes(j.kind)) {
      delete app.drafts[j.episode];
      app.dirty[j.episode] = false;
    }
    await refresh();
  }
}

// ── старт ───────────────────────────────────────────────────────────────────
window.addEventListener("hashchange", go);
go().then(pollJob);
