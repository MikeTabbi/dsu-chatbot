# Chat widget

The chat window DSU adds to its website. A red **Ask DSU** button sits in the bottom-right corner
and opens a chat panel (full screen on phones). Students type a question. The widget sends it to
the backend's `POST /chat` and shows the answer with its sources as small cards (title, link, and
"Last updated" date).

It's plain JavaScript and CSS: no framework, no npm, no build step. Edit the files and reload.

| File | What it does |
|---|---|
| `dsu-chat.js` | Everything: the button, the panel, the calls to `/chat` and `/feedback`, Markdown rendering |
| `dsu-chat.css` | All styles, including the branding colors |
| `demo.html` | A test page that loads the widget like dsu.edu would |
| `dsu-chat-mock.js` | Saved `/chat` replies for mock mode (demo page only, never embedded) |
| `tests/render.test.js` | Checks that answer text can't inject HTML or `javascript:` links |
| `tests/mock.test.js` | Checks that mock mode, thumbs up/down included, never makes a network request |

## Embedding it on a site (Drupal)

Host `dsu-chat.js` and `dsu-chat.css` in the same folder, then add one tag to the page (in Drupal,
for example in a custom block or the theme's footer):

```html
<script src="https://YOUR-HOST/widget/dsu-chat.js?v=0.1.1"
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

All colors and the font are CSS variables. Set them on the host page, for example:

```css
:root {
  --dsu-chat-accent: #a6192e;      /* button, your messages, links */
  --dsu-chat-accent-text: #ffffff; /* text on the accent color */
  --dsu-chat-font: "Open Sans", Arial, sans-serif;
}
```

The full list is at the top of [dsu-chat.css](dsu-chat.css). Keep text at a contrast ratio of at
least 4.5:1 against its background (check with https://webaim.org/resources/contrastchecker/).

The widget draws itself inside a Shadow DOM, so the site's own CSS can't change it by accident.
Only these variables cross that boundary.

## How it behaves

- **One question at a time:** each question is sent on its own. The backend doesn't get earlier
  questions or answers, so a follow-up like "how much is it?" won't know what "it" means. The
  welcome message tells students to include the details each time.
- **Answers:** shown with basic Markdown: **bold**, *italic*, bullet and numbered lists, and links.
- **Sources:** one card per cited page. The answer text doesn't repeat them (the system prompt no
  longer asks for a "Source:" line).
- **Feedback:** each answer has thumbs up and thumbs down buttons ("Helpful" / "Not helpful" for
  screen readers) that send the answer's `request_id` to the backend's `POST /feedback`. The
  chosen one gets `aria-pressed="true"` and a short "Thanks!" appears; picking the other one
  changes the rating. The styling is a placeholder; restyle with the classes `.feedback`,
  `.feedback-button`, `.feedback-up`, `.feedback-down`, and `.feedback-status`.
- **Notice:** the panel says it's an AI assistant that answers from DSU's website and can make
  mistakes, not to share personal information, and to contact the office for official decisions.
- **Errors:** a too-long or empty question shows a message under the box without calling the
  API. If the API is unavailable (503), the network fails, or the request takes over 60 seconds,
  the widget says so in plain words and puts the question back in the box to send again. When the
  API says to slow down (429) or that it's busy, the widget shows the API's own message.
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
like `[fake answer] ...`. With no `data/index/chunks.json` (see the main README), every question gets the
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
| Too many questions (429), the API's "please wait" message | "rate limit" |
| Unavailable (503) | "503" |
| Network error | "network" |
| Question too long, as the API reports it | "too long" |
| Question too long, the message under the box | the last button (fills in 1,001 characters) |

The replies are real `/chat` answers saved in [dsu-chat-mock.js](dsu-chat-mock.js). That file
replaces `fetch` on the demo page, so nothing is sent anywhere. Only `demo.html` loads it, and it
does nothing without `?mock=1`. Thumbs up/down work on every mock answer and always say "Thanks!";
the rating is kept nowhere. `dsu-chat.js` doesn't know about it, so the embed code can't turn
mock mode on. Don't host `dsu-chat-mock.js` with the widget.

## Tests

```bash
node --test widget/tests/*.test.js
```

The tests run with Node 18 or newer and need no `npm install`. CI runs them on every pull
request.

## Not built yet

- A comment box with thumbs down (`/feedback` already accepts an optional `comment`).
- Conversation memory (follow-up questions).
