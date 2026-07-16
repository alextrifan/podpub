# Dicepunk / d2 Дідів — Podcast Publishing Pipeline
### Технічна специфікація для Claude Code

Мета цього документа — дати Claude Code повний контекст, щоб зібрати автоматизований конвеєр публікації подкасту з мінімальним ручним втручанням. Проза українською, ідентифікатори коду / команди / технічні терміни — англійською (стандартна практика).

---

## 0. Контекст і мета

Автоматизуємо публікацію епізодів подкасту, який виходить під двома умовними назвами серій — **Dicepunk** і **d2 Дідів** — в межах одного фіду/акаунту (дистрибуція через Transistor → Spotify/Apple/Amazon + інші, плюс відео-версія на YouTube, плюс анонс у Telegram). Серія обирається явно прапорцем `--series` при запуску `prepare`.

**Обсяг:** 1–2 епізоди на тиждень. Це важливо: жодних проблем із квотами YouTube, жодного bulk-беклогу, оптимізуємо під надійність і зручність ревʼю, а не під масштаб.

**Що замінюємо:** ручний цикл через Descript + Transistor UI + ручне складання описів/тегів/Telegram. Descript як інструмент прибираємо повністю (рендер відео робить ffmpeg, транскрипцію — API/локальна модель).

**Ключова вимога:** обовʼязковий **крок погодження** перед будь-якою публічною публікацією. Нічого не йде в ефір без погляду очима.

---

## 1. Архітектурні принципи

Дотримуватись цих принципів при імплементації — вони важливіші за будь-яку конкретну деталь нижче.

1. **Детермінований конвеєр + Claude лише в точках судження.** Механіка (транскрипція, ffmpeg, API-аплоади, постинг) — це звичайний код. Claude викликається тільки там, де є письмо/судження: витяг ігор, метадані, текст Telegram. Не «агентити» те, що є простим пайплайном.
2. **Розділення `prepare` / `publish` / `telegram`.** Це одночасно і гейт погодження, і механізм ідемпотентності. `prepare` готує все і зупиняється; людина ревʼюить; `publish` виконує публікацію.
3. **Per-episode робоча директорія = чекпоінти.** Кожен епізод має теку `episodes/<id>/` з проміжними артефактами. Падіння на кроці N не змушує переганяти кроки 1..N-1. Повторний запуск пропускає вже зроблене (`--force` для перезапису).
4. **Ніякого автопаблишу і ніяких «тихих» часткових публікацій.** Якщо публікація на одну платформу впала — зупинитись, повідомити ясно, не робити вигляд, що все ок.
5. **Модульність провайдерів.** Транскрипція — за інтерфейсом (`Transcriber`), щоб легко міняти Whisper API ↔ Deepgram ↔ локальна модель.
6. **Два етапи публікації розділені в часі.** Причина — тайминг лінків платформ (див. §10). `publish` (Transistor + YouTube) і `telegram` — окремі запуски, часто з інтервалом у день.

---

## 2. Стек і залежності

- **Python 3.11+**
- **CLI:** `typer` (або `click`)
- **HTTP:** `httpx`
- **Anthropic SDK:** `anthropic` (виклики Claude)
- **YouTube:** `google-api-python-client`, `google-auth-oauthlib`
- **Транскрипція (дефолт):** `openai` (Whisper API) АБО `faster-whisper` (локально, без марж. вартості)
- **ffmpeg:** системний бінарник (не Python-обгортка; викликати через `subprocess`)
- **Config:** `pyyaml`, `python-dotenv`
- **Ретраї:** `tenacity`

Менеджер залежностей на вибір Claude Code (`uv` бажано, або `pip` + `requirements.txt`).

---

## 3. Структура проєкту

```
podpub/
├── .env.example
├── .gitignore                 # episodes/, .env, *.token, __pycache__
├── README.md
├── pyproject.toml
├── config.yaml                # бренд-налаштування (не секрети)
├── games_reference.json       # довідник відомих НРІ-систем для звірки
├── assets/
│   └── background_default.png # фон для відео за замовчуванням
├── templates/                 # твої стандартні формати (Ukrainian)
│   ├── podcast_description.md
│   ├── youtube_description.md
│   └── telegram_post.md
├── prompts/                   # системні промпти для Claude-викликів
│   ├── extract_games.md
│   ├── generate_metadata.md
│   └── compose_telegram.md
├── podpub/
│   ├── __init__.py
│   ├── cli.py                 # entrypoint: prepare / publish / telegram / status
│   ├── config.py              # завантаження config.yaml + .env
│   ├── store.py               # per-episode робоча тека, чекпоінти
│   ├── transcribe.py          # Transcriber (Protocol) + реалізації
│   ├── extract.py             # Claude: витяг + звірка ігор
│   ├── metadata.py            # Claude: генерація метаданих
│   ├── video.py               # ffmpeg рендер
│   ├── transistor.py          # Transistor API client
│   ├── youtube.py             # YouTube Data API client
│   ├── telegram.py            # Telegram Bot posting
│   └── links.py               # резолвінг лінків платформ
└── episodes/                  # gitignored; напр. episodes/042/...
```

---

## 4. Конфігурація і секрети

### `config.yaml` (в репозиторії, без секретів)

```yaml
show:
  transistor_show_id: "12345"          # ID шоу в Transistor (один фід, обидві серії)
  language: "uk"
  series:                               # умовні назви серій; обирається через --series
    dicepunk: "Dicepunk"
    d2didiv: "d2 Дідів"

video:
  resolution: "1920x1080"
  fps: 30
  waveform_mode: "cline"                # cline | line | p2p
  waveform_colors: "0xE0E0E0"           # колір хвилі під бренд
  waveform_height: 260
  default_background: "assets/background_default.png"

youtube:
  category_id: "24"                     # 24 = Entertainment (уточнити)
  privacy_status: "public"              # для publish; prepare нічого не постить
  made_for_kids: false

telegram:
  channel_id: "@chorna_skrynia"         # або числовий ID
  parse_mode: "HTML"

claude:
  model: "claude-sonnet-5"              # дефолт; для макс. якості метаданих можна claude-opus-4-8
  max_tokens: 4000

transcription:
  provider: "whisper_api"               # whisper_api | deepgram | faster_whisper_local
```

### `.env.example` (секрети — реальний `.env` у .gitignore)

```
ANTHROPIC_API_KEY=
OPENAI_API_KEY=                 # якщо provider=whisper_api
DEEPGRAM_API_KEY=               # якщо provider=deepgram
TRANSISTOR_API_KEY=
TELEGRAM_BOT_TOKEN=
YOUTUBE_CLIENT_SECRET_PATH=./client_secret.json
YOUTUBE_TOKEN_PATH=./youtube.token       # тут збережеться refresh token після першої авторизації
```

### Як отримати креденшели

- **ANTHROPIC_API_KEY** — console.anthropic.com.
- **Transcription** — OpenAI API key (Whisper `whisper-1`), або Deepgram key, або нічого для локальної `faster-whisper` (large-v3).
- **TRANSISTOR_API_KEY** — Transistor → Account → Your API Key. Base URL: `https://api.transistor.fm/v1`, auth через header `x-api-key`.
- **YouTube** — Google Cloud Console: створити проєкт → увімкнути **YouTube Data API v3** → OAuth consent screen (External, режим Testing, додати свій акаунт у Test users) → створити OAuth client типу **Desktop** → завантажити `client_secret.json`. Перша авторизація — одноразовий local flow, що збереже refresh token у `youtube.token`.
- **TELEGRAM_BOT_TOKEN** — створити бота через `@BotFather`, отримати токен, додати бота **адміном** у канал.

---

## 5. Потік даних

```
STAGE 1 (публікація на подкаст-платформи + YouTube)

  audio.mp3
     │
     ▼
  ┌──────────────┐   podpub prepare --audio ep.mp3 --episode 042 --series dicepunk
  │  transcribe  │──► transcript.txt
  └──────┬───────┘
         ▼
  ┌──────────────┐   Claude
  │ extract games│──► games.json  {matched:[...], uncertain:[...]}
  └──────┬───────┘
         ▼
  ┌──────────────┐   Claude + templates
  │  metadata    │──► metadata.json  {podcast:{...}, youtube:{...}}
  └──────┬───────┘
         ▼
  ┌──────────────┐   ffmpeg
  │ render video │──► video.mp4
  └──────┬───────┘
         ▼
  ┌──────────────┐
  │  review.md   │  ◄── ЗУПИНКА. Людина читає, редагує metadata.json, дивиться video.mp4
  └──────┬───────┘
         ▼                          podpub publish --episode 042
  ┌──────────────┐   Transistor API + YouTube API (метадані з metadata.json)
  │   publish    │──► published.json {transistor_id, transistor_url, youtube_id, youtube_url}
  └──────────────┘


STAGE 2 (анонс у Telegram) — окремий запуск, зазвичай наступного дня

  ┌──────────────┐   podpub telegram --episode 042
  │ resolve links│──► усі доступні посилання (див. §10)
  └──────┬───────┘
         ▼
  ┌──────────────┐   Claude + template
  │ compose post │──► telegram.md  ◄── ЗУПИНКА за замовчуванням (draft)
  └──────┬───────┘
         ▼                          podpub telegram --episode 042 --publish
  ┌──────────────┐   Telegram Bot API
  │  send post   │──► у канал
  └──────────────┘
```

---

## 6. CLI-команди

```
podpub prepare  --audio <path> --episode <id> --series dicepunk|d2didiv [--title-hint "..."] [--bg <path>] [--force]
    Транскрибує, витягує ігри, генерує метадані, рендерить відео.
    Пише все в episodes/<id>/. НІЧОГО не публікує. Завершується виводом шляху до review.md.

podpub publish  --episode <id> [--only transistor|youtube]
    Читає (можливо відредаговану) metadata.json + video.mp4.
    Публікує аудіо+метадані в Transistor і відео+метадані на YouTube.
    Пише published.json. Ідемпотентно: якщо щось уже опубліковано — пропускає.

podpub telegram --episode <id> [--publish]
    STAGE 2. Резолвить посилання, генерує текст поста в telegram.md.
    Без --publish: лише draft (за замовчуванням). З --publish: відправляє в канал.

podpub status   --episode <id>
    Показує, які кроки виконані (за наявністю файлів у робочій теці).
```

---

## 7. Компоненти (модулі)

### 7.1 `transcribe.py`
Інтерфейс:
```python
class Transcriber(Protocol):
    def transcribe(self, audio_path: str, language: str = "uk") -> str: ...
```
Реалізації: `WhisperAPITranscriber` (OpenAI `whisper-1`, дефолт), `DeepgramTranscriber`, `FasterWhisperLocal` (large-v3, offline). Фабрика вибирає за `config.transcription.provider`. Повертає plain-text транскрипт → `transcript.txt`.

### 7.2 `extract.py` — Claude
Вхід: `transcript.txt` + `games_reference.json`. Виклик Claude (промпт `prompts/extract_games.md`), структурований JSON-вихід:
```json
{
  "matched":   ["Mothership", "Blades in the Dark"],
  "uncertain": ["Дельта Грін?"]
}
```
`matched` — назви, що збіглися з довідником (канонізовані). `uncertain` — згадки, яких немає в довіднику АБО де транскрипція виглядає викривленою (типова проблема — англ. назви в укр. потоці). Саме `uncertain` виводиться в `review.md` для перевірки людиною. Записує `games.json`.

### 7.3 `metadata.py` — Claude
Вхід: `transcript.txt` + `games.json` + твої шаблони з `templates/`. Виклик Claude (промпт `prompts/generate_metadata.md`) у твоєму стандартному SEO-форматі та брендовому голосі. Вихід — `metadata.json`:
```json
{
  "podcast": {
    "title": "...",
    "description": "...",
    "keywords": ["...", "..."]
  },
  "youtube": {
    "title": "...",
    "description": "...",
    "tags": ["...", "..."]
  },
  "games_mentioned": ["..."]
}
```
Це **єдине джерело правди**, яке людина редагує між `prepare` і `publish`.

### 7.4 `video.py` — ffmpeg
Статичне зображення + анімована аудіохвиля. Команда (параметризувати з `config.video`):
```bash
ffmpeg -loop 1 -i {background} -i {audio} -filter_complex \
  "[1:a]showwaves=s={width}x{wave_h}:mode={mode}:colors={colors}:rate={fps}[w];\
   [0:v]scale={width}:{height},setsar=1[bg];\
   [bg][w]overlay=(W-w)/2:H-h-40[v]" \
  -map "[v]" -map 1:a -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 192k \
  -shortest -r {fps} {out}
```
Вихід — `video.mp4`. Заголовок/номер епізоду в саме відео **не** запікаємо (заголовок живе в метаданих YouTube) — простіше і без залежностей. Це опція на потім.

### 7.5 `transistor.py` — API client
Base URL `https://api.transistor.fm/v1`, header `x-api-key`. Флоу створення епізоду (**звірити з актуальними доками при імплементації**):
1. `GET /episodes/authorize_upload?filename=...` → отримати pre-signed `upload_url` + `audio_url`.
2. `PUT` аудіо-байти на `upload_url`.
3. `POST /episodes` з `show_id`, `audio_url`, `title`, `summary`/`description`, `keywords`.
4. `PATCH /episodes/{id}/publish` (status=published) — публікація.

Повертає `episode_id`, `share_url` (стабільний, доступний одразу). Зберігає в `published.json`.

### 7.6 `youtube.py` — Data API v3
OAuth flow (`InstalledAppFlow`), токен кешується у `YOUTUBE_TOKEN_PATH`. `videos.insert` з `snippet` (title, description, tags, categoryId) + `status` (privacyStatus з config, madeForKids=false) + медіа-аплоад `video.mp4`. Повертає `videoId` → `youtube_url = https://youtu.be/{videoId}`. При 1–2 епізодах/тиждень квота (10k/день, insert ≈1600) не проблема.

### 7.7 `telegram.py` — Bot posting
`POST https://api.telegram.org/bot{TOKEN}/sendMessage` з `chat_id`, `text`, `parse_mode`, `disable_web_page_preview=false`. Викликається лише з `--publish`.

### 7.8 `links.py` — резолвінг лінків
Збирає всі посилання на епізод (див. стратегію в §10): Transistor `share_url`, YouTube URL, і — якщо вже доступні — deep links Spotify/Apple/Amazon; інакше фолбек на show-level посилання.

### 7.9 `store.py` — робоча тека / чекпоінти
Керує `episodes/<id>/`. Хелпери: `path(episode, filename)`, `exists(...)`, `load_json/save_json`, `is_step_done(...)`. Кожен крок перевіряє наявність свого вихідного файлу і пропускає, якщо є (крім `--force`).

---

## 8. Claude-виклики (промпти)

Три виклики, усі через Anthropic SDK, модель із `config.claude.model`, **структурований JSON-вихід** (в системному промпті чітко: «поверни ЛИШЕ валідний JSON, без преамбули і markdown-огорожі»), парсити безпечно (try/except, при потребі зачистити ```json).

- **`extract_games.md`** — на вхід транскрипт + перелік відомих систем із `games_reference.json`; на вихід `{matched, uncertain}`. Інструкція: канонізувати назви за довідником; усе, чого немає в довіднику або що виглядає як спотворена транскрипція, — у `uncertain`.
- **`generate_metadata.md`** — на вхід транскрипт + список ігор + вміст `templates/podcast_description.md` і `templates/youtube_description.md`; на вихід метадані у твоєму форматі та голосі (атмосферний, не клікбейтний — як у бренді). Дотримуватись структури шаблонів.
- **`compose_telegram.md`** — на вхід опис епізоду (`metadata.podcast.description`) + `templates/telegram_post.md` + резолвнуті посилання; на вихід — готовий текст поста у твоєму стандартному форматі.

> Примітка: у `templates/` і `prompts/` кладеться **твій** реальний контент/голос. Claude Code створює файли-заготовки; ти наповнюєш їх своїми еталонними форматами (у тебе вони вже є).

`games_reference.json` — формат:
```json
[
  {"canonical": "Mothership", "aliases": ["Мазершип", "Материнський корабель"]},
  {"canonical": "Delta Green", "aliases": ["Дельта Грін"]},
  {"canonical": "Blades in the Dark", "aliases": ["Клинки в темряві"]}
]
```

---

## 9. Гейт погодження — механіка (критично)

Гейт реалізований через розділення команд + людино-читабельний артефакт.

1. `prepare` в кінці генерує `episodes/<id>/review.md` — короткий підсумок для очей:
   - згенеровані title/description/tags для **обох** платформ;
   - `games_mentioned` і окремо **`games_uncertain`** (те, що варто перевірити);
   - шлях до `video.mp4` (глянути);
   - нагадування: «відредагуй `metadata.json` за потреби, тоді запусти `podpub publish --episode <id>`».
2. Людина читає `review.md`, за потреби **редагує `metadata.json`** (це джерело правди), дивиться відео.
3. `publish` перечитує `metadata.json` (з правками) і тільки тоді публікує.

Для Telegram — аналогічно: `telegram` без `--publish` пише `telegram.md` (draft); відправка лише з `--publish`. За замовчуванням нічого в канал не йде.

---

## 10. Проблема лінків платформ (Stage 2) — важливо

Коли епізод публікується на Transistor, deep links на **Spotify/Apple/Amazon конкретного епізоду не зʼявляються миттєво** — платформи тягнуть RSS на власному розкладі (хвилини–години). Тому Telegram-крок винесено в окремий етап.

**Стратегія резолвінгу (`links.py`):**
- **Доступно одразу:** Transistor `share_url` (стабільна лендінг-сторінка епізоду, яка сама лістить усі платформи) + YouTube URL + пряме MP3.
- **Deep links платформ:** спробувати отримати з Transistor API (перевірити, чи віддає distribution-URL); якщо для епізоду ще не резолвиться — фолбек на **show-level** посилання (сторінки шоу на Spotify/Apple/Amazon, задані в config).
- **Прагматичний дефолт:** у Telegram-пості завжди даємо `share_url` Transistor (він і є універсальним «слухати всюди») + YouTube; deep links додаємо, лише якщо вже резолвнулись.

Оскільки Stage 2 — окремий ручний запуск, тайминг вирішується сам собою: **запускай `telegram` через день після `publish`**, коли лінки вже розійшлись.

---

## 11. Обробка помилок і ретраї

- Усі мережеві виклики (Claude, транскрипція, Transistor, YouTube, Telegram) — обгорнути в retry з експоненційним backoff (`tenacity`) на транзієнтні помилки (5xx, таймаути, rate limit).
- **Ідемпотентність:** перед публікацією перевіряти `published.json` — не публікувати повторно те, що вже має ID.
- **Fail loudly:** при неусувній помилці — зупинитись із зрозумілим повідомленням (що саме, на якому кроці, що вже встигло опублікуватись). Ніяких «тихих» часткових станів.
- Логувати кожен крок (стандартний `logging`, рівень INFO) — щоб було видно, де стало.

---

## 12. Порядок збірки (milestones)

Будувати і тестувати поетапно, не все одразу.

- **M1 — `prepare` + `review`.** transcribe → extract → metadata → video → review.md. Тестувати на реальному аудіо, поки метадані й відео не виходять якісними. (Найбільша цінність і найшвидша віддача.)
- **M2 — `publish`.** Спершу Transistor окремо (`--only transistor`), потім YouTube (`--only youtube`), потім разом. Перевірити OAuth-флоу YouTube і authorize-upload флоу Transistor на живому епізоді (можна приватним/unlisted для тесту).
- **M3 — `telegram`.** Резолвінг лінків + генерація поста (draft), і лише в кінці — `--publish`.

Кожен milestone має працювати і бути протестованим до переходу до наступного.

---

## 13. Що звірити з живими доками при імплементації

API з часом змінюються — не покладатись на памʼять, звірити при написанні коду:
- **Transistor API** — точні ендпоінти authorize_upload / create / publish, назви полів, чи віддає distribution/deep-link дані.
- **YouTube Data API v3** — актуальні поля `snippet`/`status`, ліміти, вимоги OAuth consent screen.
- **Anthropic model string** — актуальний ідентифікатор моделі для SDK (в config, легко змінити).
- **Telegram Bot API** — `sendMessage` параметри, вимоги до `parse_mode` (екранування для HTML/MarkdownV2).

---

## Підсумок для Claude Code

Збери проєкт за структурою §3, дотримуючись принципів §1. Серце — розділення `prepare`/`publish`/`telegram` (§6), що дає і гейт погодження (§9), і ідемпотентність через per-episode робочу теку (§7.9). Claude викликається у трьох точках (§8): витяг ігор із флагом сумнівних, генерація метаданих у брендовому форматі, текст Telegram. Descript не потрібен — відео робить ffmpeg (§7.4). Публікацію на подкаст-платформи та YouTube (Stage 1) і анонс у Telegram (Stage 2) навмисно розділено в часі через тайминг лінків платформ (§10). Починай з M1 (§12).
