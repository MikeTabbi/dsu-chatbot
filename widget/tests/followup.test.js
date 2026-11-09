// Tests for follow-up questions: the history the widget sends with each question. Run with:
// node --test widget/tests/

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const { recentTurns, HISTORY_TURNS, MESSAGES } = require("../dsu-chat.js");

const user = (text) => ({ role: "user", text, at: 1 });
const bot = (answer) => ({ role: "bot", at: 2, data: { answer, sources: [] } });

test("history is the conversation's answered questions, oldest first", () => {
  const messages = [user("How old is DSU?"), bot("It was established in 1891.")];
  assert.deepEqual(recentTurns(messages), [
    { question: "How old is DSU?", answer: "It was established in 1891." },
  ]);
});

test("a new conversation sends no history", () => {
  assert.deepEqual(recentTurns([]), []);
  assert.deepEqual(recentTurns(undefined), []);
});

test("only the last few turns are sent, and long answers are cut", () => {
  const messages = [];
  for (let n = 1; n <= 5; n++) messages.push(user(`q${n}`), bot(`a${n}`));
  assert.equal(HISTORY_TURNS, 3); // keep equal to the backend's CHAT_HISTORY_TURNS
  assert.deepEqual(
    recentTurns(messages).map((t) => t.question),
    ["q3", "q4", "q5"]
  );
  const [turn] = recentTurns([user("q"), bot("x".repeat(5000))], 3, 100);
  assert.equal(turn.answer.length, 100);
});

test("questions without a saved answer and malformed messages are skipped", () => {
  const messages = [
    user("lost one"), // its answer failed, so the next message is another question
    user("q1"),
    bot("a1"),
    { role: "bot", data: { answer: 7 } },
    null,
    user("q2"),
    { role: "bot", data: null },
    user("q3"),
    bot("a3"),
  ];
  assert.deepEqual(recentTurns(messages), [
    { question: "q1", answer: "a1" },
    { question: "q3", answer: "a3" },
  ]);
});

test("each question is posted with the current conversation's history", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "dsu-chat.js"), "utf8");
  assert.match(source, /recentTurns\(conversation \? conversation\.messages : \[\]\)/);
  assert.match(source, /body: JSON\.stringify\(\{ question, history \}\)/);
});

test("the welcome message says Jada remembers the conversation", () => {
  assert.match(MESSAGES.welcome, /remember your last few questions in this conversation/);
  assert.doesNotMatch(MESSAGES.welcome, /don't remember/);
});
