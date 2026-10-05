# Chat widget

The chat window DSU adds to its website. A red **Ask DSU** button sits in the bottom-right corner
and opens a chat panel (full screen on phones). The panel opens on a home screen: the assistant's
avatar, "Welcome to Delaware State University! How can we help you today?" over an aerial campus
photo with a red gradient,
and **Start New Conversation** (blue), **Resume Last Conversation**, and **See Past Conversations**
(not built yet, marked "Coming soon"). Buttons in the top-right corner make the panel full screen
and close it. Students type a question. The widget sends it to
the backend's `POST /chat` and shows the answer with its sources as small cards (title, link, and
"Last updated" date).

It's plain JavaScript and CSS: no framework, no npm, no build step. Edit the files and reload.

| File | What it does |
|---|---|
| `dsu-chat.js` | Everything: the button, the panel, the call to `/chat`, Markdown rendering |
| `dsu-chat.css` | All styles, including the branding colors |
| `dsu-avatar.svg` | The assistant's illustrated avatar (launcher, header, next to answers) |
| `dsu-campus.jpg` | Aerial campus photo behind the welcome text (see "Header photo") |
| `fonts/` | Roboto font files you add (see "Font"). Optional: without them the widget uses Arial |
| `demo.html` | A test page that loads the widget like dsu.edu would |
| `dsu-chat-mock.js` | Saved `/chat` replies for mock mode (demo page only, never embedded) |
| `tests/render.test.js` | Checks that answer text can't inject HTML or `javascript:` links |
| `tests/mock.test.js` | Checks that mock mode never makes a network request |

## Embedding it on a site (Drupal)

Host `dsu-chat.js`, `dsu-chat.css`, `dsu-avatar.svg`, `dsu-campus.jpg`, and the `fonts/` folder
together, then add one tag to the page (in Drupal,
for example in a custom block or the theme's footer):

```html
<script src="https://YOUR-HOST/widget/dsu-chat.js?v=0.1.0"
        data-api-url="https://YOUR-API-HOST" defer></script>
```

| Attribute | Required | What it does |
|---|---|---|
| `data-api-url` | yes | The backend's base URL. The widget posts to `<data-api-url>/chat`. Must be `http(s)://` |
| `data-max-chars` | no | Longest question allowed (default 1000). Keep it equal to the backend's `CHAT_MAX_QUESTION_CHARS` |
| `data-title` | no | Text on the button and panel heading (default "Ask DSU") |

The backend must also allow the site's origin, or the browser blocks the request. Set
`ALLOWED_ORIGINS` on the backend to the exact sites, comma-separated, for example
`ALLOWED_ORIGINS=https://www.desu.edu`. `*` (any site) is refused at startup.

**Versioning:** `VERSION` at the top of `dsu-chat.js` is added to the CSS URL so browsers fetch
the new styles. When you change either file, bump `VERSION` and the `?v=` in the script tag.

## Branding

Colors come from the [DSU Branding Tool Kit (August 2023)](https://www.desu.edu/sites/flagship/files/document/31/dsu_style_guide.pdf):

| Brand color | Used for | Contrast |
|---|---|---|
| Red, Pantone 485 C, `#EE3124` | Header gradient over the photo, avatar shirt | Shaded with brand black (30%): white text 5.6:1 behind the welcome text, 4.7:1 at the right edge, even over a white spot in the photo |
| Darker red `#D51C28` (athletics red, not in the tool kit) | Ask DSU launcher, links, source titles | White 5.22:1, 4.8:1 on the light gray |
| Dark blue `#00549A` (chosen by the team, not in the tool kit) | Start New Conversation button, send arrow, card and source-card edges, chevrons, loading dots | White 7.9:1 |
| Blue, Pantone 299 C, `#009DDC` | Your messages, "Coming soon" badge, avatar background | Brand black text 5.32:1 |
| Black, Process Black, `#231F20` | Main text, focus ring, header shading | 16.3:1 on white |

**Font:** Roboto (the tool kit names Myriad Pro for print, which is a licensed Adobe font). The
widget doesn't load anything from Google. It uses Roboto if the visitor has it installed, or from
a `fonts/` folder next to `dsu-chat.css`:

1. Download Roboto from https://fonts.google.com/specimen/Roboto ("Get font", then "Download all").
2. From the zip's `static/` folder, copy `Roboto-Regular.ttf`, `Roboto-Medium.ttf`, and
   `Roboto-Bold.ttf` into `widget/fonts/`.

Without the files the widget falls back to Helvetica Neue or Arial, and the browser console shows
404s for the three font files. Roboto is under the SIL Open Font License, so hosting it is fine.

All colors and the font are CSS variables. Set them on the host page, for example:

```css
:root {
  --dsu-chat-accent: #d51c28;      /* launcher, Send, links */
  --dsu-chat-font: "Myriad Pro", Arial, sans-serif;
}
```

The full list is at the top of [dsu-chat.css](dsu-chat.css). Keep text at a contrast ratio of at
least 4.5:1 against its background (check with https://webaim.org/resources/contrastchecker/).

The widget draws itself inside a Shadow DOM, so the site's own CSS can't change it by accident.
Only these variables cross that boundary.

### Header photo

`dsu-campus.jpg` is an aerial photo of campus, shown under a red gradient. To change it, replace the
file (keep it under about 200 KB), or set it from the host page with a full URL:
`--dsu-chat-hero-image: url("https://www.desu.edu/.../campus.jpg");`. Check the photo's usage
rights with Marketing & Communications before going live.

## How it behaves

- **Home screen:** Start New Conversation clears the chat and shows the welcome message. Resume
  Last Conversation reopens the last chat, even after a page reload: the questions and answers
  are kept in the browser's `localStorage` (last 40 messages, never sent anywhere). Starting a new
  conversation deletes it. If storage is blocked, the chat still works but isn't kept. The back
  arrow in the chat header returns to the home screen.
- **Question box:** the character count and a round send arrow (labeled "Send" for screen
  readers) sit inside the box. Enter also sends.
- **Full screen:** the button next to the X fills the browser window. Messages sit in a centered
  column, each a little under half its width. Press it
  again to go back. Phones are always full screen, so it's hidden there.
- **One question at a time:** each question is sent on its own. The backend doesn't get earlier
  questions or answers, so a follow-up like "how much is it?" won't know what "it" means. The
  welcome message tells students to include the details each time.
- **Answers:** shown with basic Markdown: **bold**, *italic*, bullet and numbered lists, and links.
- **Sources:** one card per cited page. The answer text doesn't repeat them (the system prompt no
  longer asks for a "Source:" line).
- **Notice:** the panel says it's an AI assistant that answers from DSU's website and can make
  mistakes, not to share personal information, and to contact the office for official decisions.
- **Errors:** a too-long or empty question shows a message under the box without calling the
  API. If the API is unavailable (503), the network fails, or the request takes over 60 seconds,
  the widget says so in plain words and puts the question back in the box to send again.
- **Accessibility:** everything works with the keyboard (Tab, Enter to send, Shift+Enter for a
  new line, Escape to close). Focus is always visible. Buttons and the question box have labels,
  new answers are read out by screen readers (the conversation is an `aria-live` region), and
  links say they open in a new tab.

## Security

Answers come from an AI model reading web pages, so treat them as untrusted text:

- Answer text is **never** inserted as HTML. `renderMarkdown()` builds elements one at a time
  with `createElement` and `createTextNode`, so `<script>` or `<img onerror=...>` in an answer
  shows up as visible text.
- Links are only made for `http://` and `https://` URLs. `javascript:`, `data:`, and relative
  links stay as plain text. Links open in a new tab with `rel="noopener noreferrer"`.
- When you change the widget, don't use `innerHTML`, `outerHTML`, `insertAdjacentHTML`, or
  `document.write` with answer, source, or user text. The only `innerHTML` is the fixed panel
  layout (`SHELL`), which contains no outside text.

## Running it locally

You need the backend and a second local web server for the demo page (port 8080, a different
origin from the API, like dsu.edu will be).

```bash
# Terminal 1: the backend (from the repo root). The default ALLOWED_ORIGINS allows the demo page.
uvicorn api.app.main:app --reload                          # fake answers, no API key
CLAUDE_CLIENT=anthropic uvicorn api.app.main:app --reload  # real answers

# Terminal 2: serve the widget folder
python -m http.server 8080 --directory widget
```

Open http://localhost:8080/demo.html and click **Ask DSU**. With the fake client, answers look
like `[fake answer] ...`. With no `data/chunks` (see the main README), every question gets the
"couldn't find anything" answer.

If the widget says it couldn't reach the assistant, check that the backend is running and that
`ALLOWED_ORIGINS` (in your `.env`, if set) includes `http://localhost:8080`. The browser console
shows a CORS error when it doesn't.

To see the phone layout, open your browser's developer tools and turn on the device toolbar (a
screen narrower than 480px).

## Mock mode (no backend)

For styling work you don't need the backend or an API key:

```bash
python -m http.server 8080 --directory widget
```

Open http://localhost:8080/demo.html?mock=1. A **Mock mode** box on the page has one button per
state. Each button opens the widget and asks a question. Typing a question that contains the
word in quotes does the same:

| State | Button or word |
|---|---|
| Short answer, one source card | "short" (also any question without the other words) |
| Long answer with lists and links, two source cards | "long answer" |
| Personal-data redirect, no sources | "personal" |
| Off-topic decline | "off topic" |
| Loading dots for 5 seconds, then the short answer | "slow" |
| Unavailable (503) | "503" |
| Network error | "network" |
| Question too long, as the API reports it | "too long" |
| Question too long, the message under the box | the last button (fills in 1,001 characters) |

The replies are real `/chat` answers saved in [dsu-chat-mock.js](dsu-chat-mock.js). That file
replaces `fetch` on the demo page, so nothing is sent anywhere. Only `demo.html` loads it, and it
does nothing without `?mock=1`. `dsu-chat.js` doesn't know about it, so the embed code can't turn
mock mode on. Don't host `dsu-chat-mock.js` with the widget.

## Tests

```bash
node --test widget/tests/*.test.js
```

The tests run with Node 18 or newer and need no `npm install`. CI runs them on every pull
request.

## Not built yet

- See Past Conversations: the card is shown but does nothing yet (it says "Coming soon").
- Thumbs up/down feedback: needs a `/feedback` endpoint on the backend first.
- Conversation memory (follow-up questions).
