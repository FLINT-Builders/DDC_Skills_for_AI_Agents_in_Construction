/**
 * Authorization spec for the PTO foreman status handler in SKILL.md.
 *
 * Run:
 *   node --test 3_DDC_Insights/Automation-Workflows/n8n-pto-pipeline/status-update-authorization.test.mjs
 *
 * These tests fail while a Telegram reply can change another crew's task.
 * They pass once a reply updates a task only when all of the following hold:
 *   - the sender chat id is on the Foremen sheet
 *   - that same chat id is the task's foreman_chat_id
 *   - the replied-to message id is that task's id
 * A sender who is a foreman for a different task, or who is missing from the
 * Foremen sheet, must not change status or comment.
 *
 * The handler under test is the JavaScript block in SKILL.md, not a copy.
 * Data available to that block, matching this skill's own code-node pattern
 * (the filter block reads tasks from $input and the Telegram message from
 * $node["Telegram Trigger"]):
 *   $node["Telegram Trigger"].json  Telegram Update (n8n item json)
 *   $input.all()                    task rows from Get Today Tasks
 *   $("Get Today Tasks").all()      same task rows
 *   $("Foremen").all()              Foremen sheet rows { name, chat_id }
 *   $items("Get Today Tasks")       task rows
 *   $items("Foremen")               Foremen sheet rows
 * Each row is an n8n item { json: ... }. $node[name].json is the first item
 * only, which is how the Code node exposes $node.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const skillPath = fileURLToPath(new URL("./SKILL.md", import.meta.url));

const CREW_A = "111001";
const CREW_B = "222002";
const OUTSIDER = "333003";
const TASK_A = 41;
const TASK_B = 42;

function extractJavaScriptBlocks(markdown) {
  const blocks = [];
  const pattern = /```javascript\r?\n([\s\S]*?)```/g;
  for (const match of markdown.matchAll(pattern)) blocks.push(match[1]);
  return blocks;
}

function blockContaining(blocks, snippet) {
  const found = blocks.find((block) => block.includes(snippet));
  assert.ok(found, `SKILL.md is missing a javascript block containing ${snippet}`);
  return found;
}

/**
 * n8n 2.49.6 packages/core/.../utils/return-json-array.ts
 * TelegramTrigger.webhook passes the Telegram update through this helper.
 * An update has no `json` property, so the item json is the update itself.
 */
function returnJsonArray(jsonData) {
  const returnData = [];
  if (!Array.isArray(jsonData)) jsonData = [jsonData];
  jsonData.forEach((data) => {
    if (data?.json) returnData.push({ ...data, json: data.json });
    else returnData.push({ json: data });
  });
  return returnData;
}

/**
 * Restrict-to-chat / restrict-to-user gates from TelegramTrigger.webhook at
 * n8n 2.49.6 (defaultVersion 1.5). additionalFields defaults to {}.
 * This skill never sets chatIds or userIds, so both gates are inactive.
 */
function triggerForwardsUpdate(bodyData, additionalFields = {}, nodeVersion = 1.5) {
  const updates = [];
  if (nodeVersion >= 1.5) {
    if (updates.length > 0 && !updates.includes("*")) {
      const updateTypes = [
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "callback_query",
        "inline_query",
        "pre_checkout_query",
        "shipping_query",
        "poll",
      ];
      const updateType = updateTypes.find((key) => bodyData[key] !== undefined);
      if (updateType && !updates.includes(updateType)) return null;
    }
  }
  if (nodeVersion >= 1.2) {
    if (additionalFields.chatIds) {
      const splitIds = String(additionalFields.chatIds)
        .split(",")
        .map((chatId) => chatId.trim());
      const chatId =
        nodeVersion >= 1.4
          ? (bodyData.message?.chat?.id ??
            bodyData.edited_message?.chat?.id ??
            bodyData.channel_post?.chat?.id ??
            bodyData.edited_channel_post?.chat?.id ??
            bodyData.callback_query?.message?.chat?.id)
          : bodyData.message?.chat?.id;
      if (!splitIds.includes(String(chatId))) return null;
    }
    if (additionalFields.userIds) {
      const splitIds = String(additionalFields.userIds)
        .split(",")
        .map((userId) => userId.trim());
      const userId =
        nodeVersion >= 1.4
          ? (bodyData.message?.from?.id ??
            bodyData.edited_message?.from?.id ??
            bodyData.channel_post?.from?.id ??
            bodyData.edited_channel_post?.from?.id ??
            bodyData.callback_query?.from?.id ??
            bodyData.inline_query?.from?.id ??
            bodyData.pre_checkout_query?.from?.id ??
            bodyData.shipping_query?.from?.id)
          : bodyData.message?.from?.id;
      if (!splitIds.includes(String(userId))) return null;
    }
  }
  return returnJsonArray([bodyData]);
}

function itemsOf(rows) {
  return rows.map((row) => ({ json: row }));
}

function createContext({ triggerItems, tasks, foremen }) {
  const nodeItems = {
    "Telegram Trigger": triggerItems,
    "Get Today Tasks": itemsOf(tasks),
    Foremen: itemsOf(foremen),
  };
  const $node = new Proxy(
    {},
    {
      get(_target, name) {
        const nodeName = String(name);
        const items = nodeItems[nodeName];
        if (!items) {
          throw new Error(`Referenced node doesn't exist: ${nodeName}`);
        }
        return {
          get json() {
            if (items.length === 0) {
              throw new Error(`Node '${nodeName}' hasn't been executed`);
            }
            return items[0].json;
          },
        };
      },
    },
  );
  const accessor = (nodeName) => {
    const items = nodeItems[nodeName];
    if (!items) throw new Error(`Referenced node doesn't exist: ${nodeName}`);
    return {
      all() {
        return items;
      },
      first() {
        return items[0];
      },
      last() {
        return items[items.length - 1];
      },
      get json() {
        return items[0]?.json;
      },
    };
  };
  return {
    $node,
    $input: {
      all() {
        return itemsOf(tasks);
      },
      first() {
        return itemsOf(tasks)[0];
      },
      last() {
        return itemsOf(tasks)[itemsOf(tasks).length - 1];
      },
    },
    $items(nodeName) {
      const items = nodeItems[nodeName];
      if (!items) throw new Error(`Referenced node doesn't exist: ${nodeName}`);
      return items;
    },
    $(nodeName) {
      return accessor(nodeName);
    },
  };
}

function runBlock(source, context) {
  const runner = new Function("$node", "$input", "$items", "$", source);
  return runner(context.$node, context.$input, context.$items, context.$);
}

function normalizeItems(result) {
  if (result == null) return [];
  return Array.isArray(result) ? result : [result];
}

function sameId(left, right) {
  return String(left) === String(right);
}

/**
 * Apply the handler's returned items onto the task rows.
 * The handler's return shape is the Tasks sheet write: task_id, status, comment.
 * In-place edits of the rows handed to the handler count as writes too.
 */
function sheetAfterHandler(tasks, result) {
  const sheet = structuredClone(tasks);
  for (const item of normalizeItems(result)) {
    const data = item?.json ?? item;
    if (data == null || data.task_id === undefined) continue;
    const row = sheet.find((task) => sameId(task.task_id, data.task_id));
    if (!row) continue;
    if (data.status !== undefined) row.status = data.status;
    if (data.comment !== undefined) row.comment = data.comment;
  }
  return sheet;
}

function row(sheet, taskId) {
  const found = sheet.find((task) => sameId(task.task_id, taskId));
  assert.ok(found, `missing task ${taskId}`);
  return found;
}

function assertRowUnchanged(sheet, taskId, baseline) {
  const current = row(sheet, taskId);
  const original = row(baseline, taskId);
  assert.equal(current.status, original.status);
  assert.equal(current.comment, original.comment);
}

/**
 * Reply inside the sender's own private chat. message_id is unique only inside
 * that chat, so the replied-to id can equal another chat's task id.
 * reply_to_message is present only for a reply in the same chat.
 */
function privateReply({ chatId, text, replyToMessageId }) {
  return {
    update_id: 9000 + replyToMessageId,
    message: {
      message_id: replyToMessageId + 1,
      date: 1_700_000_000,
      chat: { id: Number(chatId), type: "private" },
      from: { id: Number(chatId), is_bot: false, first_name: "Sender" },
      text,
      reply_to_message: {
        message_id: replyToMessageId,
        date: 1_700_000_000,
        chat: { id: Number(chatId), type: "private" },
        from: { id: Number(chatId), is_bot: false, first_name: "Sender" },
        text: "earlier message",
      },
    },
  };
}

function crews() {
  return {
    tasks: [
      {
        task_id: TASK_A,
        task_name: "Pour footing",
        description: "Grid A",
        location: "Block A",
        deadline: "2026-11-25",
        foreman_chat_id: CREW_A,
        status: "в работе",
        comment: "",
      },
      {
        task_id: TASK_B,
        task_name: "Set rebar",
        description: "Grid B",
        location: "Block B",
        deadline: "2026-11-25",
        foreman_chat_id: CREW_B,
        status: "в работе",
        comment: "",
      },
    ],
    foremen: [
      { name: "Crew A", chat_id: CREW_A, registered_at: "2026-11-01T00:00:00.000Z" },
      { name: "Crew B", chat_id: CREW_B, registered_at: "2026-11-01T00:00:00.000Z" },
    ],
  };
}

function loadBlocks() {
  const markdown = readFileSync(skillPath, "utf8");
  const blocks = extractJavaScriptBlocks(markdown);
  return {
    statusSource: blockContaining(blocks, "reply_to_message"),
    filterSource: blockContaining(blocks, "foreman_chat_id"),
  };
}

function runStatus(source, { tasks, foremen, update }) {
  const triggerItems = triggerForwardsUpdate(update);
  assert.ok(triggerItems, "Telegram trigger dropped the update before the handler");
  assert.equal(triggerItems.length, 1);
  assert.equal(triggerItems[0].json, update);
  const baseline = structuredClone(tasks);
  const context = createContext({ triggerItems, tasks, foremen });
  let result;
  try {
    result = runBlock(source, context);
  } catch {
    result = [];
  }
  return { baseline, sheet: sheetAfterHandler(tasks, result), result };
}

test("outsider reply of выполнил does not complete another crew's task", () => {
  const { statusSource } = loadBlocks();
  const { tasks, foremen } = crews();
  const update = privateReply({
    chatId: OUTSIDER,
    text: "выполнил",
    replyToMessageId: TASK_A,
  });
  const { baseline, sheet } = runStatus(statusSource, { tasks, foremen, update });
  assertRowUnchanged(sheet, TASK_A, baseline);
  assertRowUnchanged(sheet, TASK_B, baseline);
});

test("registered foreman cannot complete the other crew's task by replying to its id", () => {
  const { statusSource, filterSource } = loadBlocks();
  const { tasks, foremen } = crews();
  const update = privateReply({
    chatId: CREW_B,
    text: "выполнил",
    replyToMessageId: TASK_A,
  });
  const triggerItems = triggerForwardsUpdate(update);
  const filterContext = createContext({ triggerItems, tasks, foremen });
  const visibleToFilter = runBlock(filterSource, filterContext);
  const visibleIds = visibleToFilter.map((item) => item.json.task_id);
  assert.deepEqual(visibleIds, [TASK_B]);

  const { baseline, sheet } = runStatus(statusSource, { tasks, foremen, update });
  assertRowUnchanged(sheet, TASK_A, baseline);
  assertRowUnchanged(sheet, TASK_B, baseline);
});

test("outsider not-done reply does not change another crew's status or comment", () => {
  const { statusSource } = loadBlocks();
  const { tasks, foremen } = crews();
  const update = privateReply({
    chatId: OUTSIDER,
    text: "не выполнил скрыл дефект",
    replyToMessageId: TASK_A,
  });
  const { baseline, sheet } = runStatus(statusSource, { tasks, foremen, update });
  assertRowUnchanged(sheet, TASK_A, baseline);
  assertRowUnchanged(sheet, TASK_B, baseline);
});

test("reply without a status word does not reset another crew's completed task", () => {
  const { statusSource } = loadBlocks();
  const { tasks, foremen } = crews();
  tasks[0].status = "выполнено";
  tasks[0].comment = "сдал";
  const update = privateReply({
    chatId: OUTSIDER,
    text: "ок",
    replyToMessageId: TASK_A,
  });
  const { baseline, sheet } = runStatus(statusSource, { tasks, foremen, update });
  assertRowUnchanged(sheet, TASK_A, baseline);
  assertRowUnchanged(sheet, TASK_B, baseline);
});

test("chat id on the task but missing from the Foremen sheet cannot update it", () => {
  const { statusSource } = loadBlocks();
  const { tasks, foremen } = crews();
  const removed = foremen.filter((foreman) => !sameId(foreman.chat_id, CREW_A));
  const update = privateReply({
    chatId: CREW_A,
    text: "выполнил",
    replyToMessageId: TASK_A,
  });
  const { baseline, sheet } = runStatus(statusSource, {
    tasks,
    foremen: removed,
    update,
  });
  assertRowUnchanged(sheet, TASK_A, baseline);
  assertRowUnchanged(sheet, TASK_B, baseline);
});

test("foreman reply to their own task id marks that task completed and leaves the other crew", () => {
  const { statusSource } = loadBlocks();
  const { tasks, foremen } = crews();
  const update = privateReply({
    chatId: CREW_A,
    text: "выполнил",
    replyToMessageId: TASK_A,
  });
  const { baseline, sheet } = runStatus(statusSource, { tasks, foremen, update });
  assert.equal(row(sheet, TASK_A).status, "выполнено");
  assert.equal(row(sheet, TASK_A).comment, "");
  assertRowUnchanged(sheet, TASK_B, baseline);
});

test("foreman listed first on the Foremen sheet still cannot write the other crew's task", () => {
  const { statusSource } = loadBlocks();
  const { tasks, foremen } = crews();
  const attackerFirst = [...foremen].reverse();
  assert.equal(String(attackerFirst[0].chat_id), CREW_B);
  const update = privateReply({
    chatId: CREW_B,
    text: "выполнил",
    replyToMessageId: TASK_A,
  });
  const { baseline, sheet } = runStatus(statusSource, {
    tasks,
    foremen: attackerFirst,
    update,
  });
  assertRowUnchanged(sheet, TASK_A, baseline);
  assertRowUnchanged(sheet, TASK_B, baseline);
});
