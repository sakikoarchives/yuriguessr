"use strict";
// Exercises the real round-selection code without a DOM dependency or network.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "..", "index.template.html"), "utf8");
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(scriptMatch, "Inline game script is missing");
function sandbox(data = []) {
  const saved = new Map();
  const store = {
    getItem: (key) => saved.get(key) ?? null,
    setItem: (key, value) => saved.set(key, String(value)),
    removeItem: (key) => saved.delete(key),
  };
  const nodes = new Map();
  class FakeClassList {
    constructor() { this.classes = new Set(); }
    add(...names) { for (const name of names) this.classes.add(name); }
    remove(...names) { for (const name of names) this.classes.delete(name); }
    contains(name) { return this.classes.has(name); }
    toggle(name, force) {
      const has = force === undefined ? !this.classes.has(name) : force;
      if (has) this.classes.add(name);
      else this.classes.delete(name);
      return has;
    }
  }
  class FakeNode {
    constructor() {
      this.classList = new FakeClassList();
      this.children = [];
      this.dataset = {};
      this.events = new Map();
      this.textContent = "";
      this.href = "";
      this.disabled = false;
    }
    addEventListener(type, callback) { this.events.set(type, callback); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    querySelectorAll(selector) {
      if (selector === ".answer") return this.children.filter((child) => child.className === "answer");
      return [];
    }
    click() { this.events.get("click")?.(); }
  }
  class FastImage {
    set src(value) {
      this.imageSource = value;
      queueMicrotask(() => {
        if (String(value).includes("BROKEN")) this.onerror?.();
        else this.onload?.();
      });
    }
  }
  const doc = {
    querySelector: (selector) => {
      if (!nodes.has(selector)) nodes.set(selector, new FakeNode());
      return nodes.get(selector);
    },
    createElement: () => new FakeNode(),
    addEventListener() {},
  };
  const context = vm.createContext({
    document: doc, window: {}, console, localStorage: store, Image: FastImage,
    setTimeout, clearTimeout,
  });
  const script = scriptMatch[1]
    .replace("__GAME_DATA__", JSON.stringify(data))
    .replace("__BUILD_META__", "{}")
    .replace(/\n\s*startGame\(\);\s*$/, "\n  // Disabled only for selection tests.");
  vm.runInContext(script, context, { filename: "index.template.html" });
  const state = vm.runInContext("state", context);
  return { context, state, store, nodes };
}

function posts(tag, first, count) {
  return Array.from({ length: count }, (_, i) => ({
    id: first + i, artist: `${tag}_artist_${i}`, imageData: `images/${first + i}.webp`,
    poolTag: tag, postUrl: `https://danbooru.donmai.us/posts/${first + i}`,
  }));
}

test("no post repeats, all pools drain without silently resetting the used set", () => {
  const all = [...posts("Genshin", 1, 7), ...posts("ZZZ", 101, 7), ...posts("Honkai", 201, 7)];
  const { context, state } = sandbox(all);
  state.pool = all;
  state.availablePools = vm.runInContext("validateData()", context);
  assert.equal(state.availablePools.length, 3);
  const seen = new Set();
  for (let n = 0; n < all.length; n++) {
    const round = vm.runInContext("buildNextRound()", context);
    assert.ok(round, `No round available at index ${n}`);
    assert.ok(!seen.has(round.id), `Repeated post ${round.id}`);
    assert.equal(round.choices.length, 4);
    assert.equal(new Set(round.choices).size, 4);
    assert.ok(round.choices.includes(round.artist));
    seen.add(round.id);
    state.reservedIds.delete(round.id);
    state.seenIds.add(round.id);
    state.currentRound = round;
  }
  assert.equal(seen.size, all.length);
  assert.equal(vm.runInContext("buildNextRound()", context), null);
  assert.equal(state.seenIds.size, all.length);
});

test("reused post IDs from multiple category tags cannot cause repetition", () => {
  const all = [...posts("Genshin", 1, 5), ...posts("ZZZ", 101, 5)];
  all.push({ ...all[0], poolTag: "ZZZ" });
  const { context, state } = sandbox(all);
  state.pool = all;
  state.availablePools = vm.runInContext("validateData()", context);
  assert.equal(state.poolByTag.get("ZZZ").length, 5);
  const chosen = new Set();
  for (let i = 0; i < 10; i++) {
    const round = vm.runInContext("buildNextRound()", context);
    assert.ok(!chosen.has(round.id));
    chosen.add(round.id);
    state.reservedIds.delete(round.id);
    state.seenIds.add(round.id);
  }
  assert.equal(chosen.size, 10);
  assert.equal(vm.runInContext("buildNextRound()", context), null);
});

test("prefetch reservations never reuse queued artwork", () => {
  const all = posts("Genshin", 1, 5);
  const { context, state } = sandbox(all);
  state.pool = all;
  state.availablePools = vm.runInContext("validateData()", context);
  const first = vm.runInContext("buildNextRound()", context);
  const second = vm.runInContext("buildNextRound()", context);
  assert.notEqual(first.id, second.id);
  assert.equal(state.reservedIds.size, 2);
});

test("history persists across replays and can be reset deliberately", () => {
  const { context, state, store } = sandbox();
  state.seenIds.add(123);
  state.seenIds.add(456);
  vm.runInContext("saveSeenHistory()", context);
  assert.deepEqual(JSON.parse(store.getItem("yuriguessr-seen-artworks-v1")), [123, 456]);
  state.seenIds.clear();
  const loaded = vm.runInContext("loadSeenHistory()", context);
  assert.ok(loaded.has(123) && loaded.has(456));
  vm.runInContext("forgetSeenHistory()", context);
  assert.equal(state.seenIds.size, 0);
  assert.equal(store.getItem("yuriguessr-seen-artworks-v1"), null);
});

test("no cropped fixed-height frame styling remains", () => {
  const style = html.match(/\.artwork-frame\s*\{([^}]+)\}/)?.[1] || "";
  assert.doesNotMatch(style, /overflow:\s*hidden/);
  assert.doesNotMatch(style, /height:\s*min\(/);
  const imageStyle = html.match(/\.artwork-frame img\s*\{([^}]+)\}/)?.[1] || "";
  assert.match(imageStyle, /max-width:\s*100%/);
  assert.match(imageStyle, /height:\s*auto/);
  assert.match(imageStyle, /object-fit:\s*contain/);
});

async function until(predicate, what) {
  for (let i = 0; i < 200; i++) {
    if (predicate()) return;
    await new Promise((done) => setTimeout(done, 3));
  }
  assert.fail(`Timed out while waiting for ${what}`);
}

test("full gameplay: 15 unique rounds, scoring, exhaustion panel, explicit replay", async () => {
  const data = [...posts("Genshin", 1, 5), ...posts("ZZZ", 101, 5), ...posts("Honkai", 201, 5)];
  const { context, state, nodes, store } = sandbox(data);
  vm.runInContext("CONFIG.correctAdvanceDelay = 1; CONFIG.wrongAdvanceDelay = 1", context);
  await vm.runInContext("startGame()", context);
  const ids = new Set();
  const answers = nodes.get("#answers");
  for (let index = 0; index < data.length; index++) {
    await until(() => !state.locked && state.roundNumber === index + 1, `round ${index + 1}`);
    const round = state.currentRound;
    assert.ok(!ids.has(round.id), `Round ${index + 1} repeats ${round.id}`);
    ids.add(round.id);
    assert.equal(answers.children.length, 4);
    const button = answers.children.find((child) => child.dataset.artist === round.artist);
    assert.ok(button, "Correct answer is present");
    button.click();
    assert.equal(state.locked, true);
    assert.equal(nodes.get("#feedback").classList.contains("hidden"), false);
    assert.ok(nodes.get("#feedbackTitle").textContent.startsWith("Correct"));
    if (index === 0) assert.equal(state.score, 200);
  }
  await until(() => !nodes.get("#finishPanel").classList.contains("hidden"), "exhaustion panel");
  assert.equal(nodes.get("#finishTitle").textContent, "All artworks seen!");
  assert.equal(nodes.get("#restartButton").textContent, "Replay this collection");
  assert.equal(ids.size, data.length);
  assert.equal(state.seenIds.size, data.length);
  assert.equal(JSON.parse(store.getItem("yuriguessr-seen-artworks-v1")).length, data.length);

  // Only an explicit click on Replay may clear already seen posts.
  nodes.get("#restartButton").click();
  await until(() => !state.locked && state.roundNumber === 1, "restarted round");
  assert.equal(state.score, 100);
  assert.equal(state.streak, 0);
  assert.equal(state.seenIds.size, 1);
});

test("wrong answers still penalize and Game over restart does not recycle artwork", async () => {
  const data = posts("Genshin", 1001, 10);
  const { context, state, nodes } = sandbox(data);
  vm.runInContext("CONFIG.correctAdvanceDelay = 1; CONFIG.wrongAdvanceDelay = 1; CONFIG.gameOverDelay = 1", context);
  await vm.runInContext("startGame()", context);
  const ids = [];
  for (let index = 0; index < 3; index++) {
    await until(() => !state.locked && state.roundNumber === index + 1, `wrong round ${index + 1}`);
    ids.push(state.currentRound.id);
    const incorrect = nodes.get("#answers").children.find((button) => button.dataset.artist !== state.currentRound.artist);
    assert.ok(incorrect);
    incorrect.click();
    assert.equal(state.score, Math.max(0, 100 - (index + 1) * 35));
    assert.equal(state.streak, 0);
    assert.ok(incorrect.classList.contains("wrong"));
  }
  await until(() => !nodes.get("#finishPanel").classList.contains("hidden"), "game-over panel");
  assert.equal(nodes.get("#finishTitle").textContent, "Game over");
  assert.equal(nodes.get("#restartButton").textContent, "Play again");
  assert.equal(new Set(ids).size, 3);
  nodes.get("#restartButton").click();
  await until(() => !state.locked && state.roundNumber === 1, "restart after loss");
  assert.equal(state.score, 100);
  assert.ok(!ids.includes(state.currentRound.id), "Restart unexpectedly repeated a previous image");
});


test("number-key choice keeps the original keyboard controls", async () => {
  const data = posts("Genshin", 2001, 5);
  const { context, state, nodes } = sandbox(data);
  vm.runInContext("CONFIG.correctAdvanceDelay = 1; CONFIG.wrongAdvanceDelay = 1", context);
  await vm.runInContext("startGame()", context);
  assert.equal(state.locked, false);
  vm.runInContext("handleKeyboard({ key: '1', repeat: true })", context);
  assert.equal(state.locked, false, "Repeated keydown should be ignored");
  const firstButton = nodes.get("#answers").children[0];
  const shouldBeCorrect = firstButton.dataset.artist === state.currentRound.artist;
  vm.runInContext("handleKeyboard({ key: '1', repeat: false })", context);
  assert.equal(state.locked, true);
  assert.equal(state.score, shouldBeCorrect ? 200 : 65);
});
