/**
 * Spec for the BIM Change Detection workflow in
 * 5_DDC_Innovative/n8n-workflow-automation/SKILL.md.
 *
 * These tests fail while that example places JSON.stringify($json) inside a
 * SQL string literal. They pass when the logged change set is stored as data
 * and element text cannot change the statement Postgres runs.
 *
 * Versions are the ones n8n 2.49.5 ships (the unpinned `npm install n8n -g`
 * / `n8nio/n8n` install in the skill, resolved on 2026-11-23):
 *   @n8n/tournament 1.16.0
 *   pg-promise 11.9.1
 *   pg 8.21.0
 *
 * The harness follows those packages and n8n@2.49.5:
 *   packages/nodes-base/utils/utilities.ts getResolvables
 *   packages/core/.../node-execution-context.ts evaluateExpression
 *   packages/workflow/src/expression.ts initializeGlobalContext + extendSyntax
 *   packages/workflow/src/workflow-data-proxy.ts getDataProxy ($json, has trap)
 *   packages/nodes-base/nodes/Postgres/v2/actions/database/executeQuery.operation.ts
 *   packages/nodes-base/nodes/Postgres/v2/helpers/utils.ts configureQueryRunner
 *   pg-promise lib/helpers/methods/concat.js and lib/database.js multi()
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { after, describe, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import pgPromise from 'pg-promise';
import { Tournament } from '@n8n/tournament';

const here = dirname(fileURLToPath(import.meta.url));
const skillPath = join(here, '../../5_DDC_Innovative/n8n-workflow-automation/SKILL.md');

const DROP_NAME = "x'); DROP TABLE bim_projects; --";
const QUOTE_NAME = "O'Brien";
const SUBQUERY_NAME = "x' || (SELECT name FROM bim_projects LIMIT 1) || '";

function loadWorkflow() {
  const markdown = readFileSync(skillPath, 'utf8');
  const blocks = markdown.matchAll(/```json\n([\s\S]*?)```/g);
  for (const block of blocks) {
    let parsed;
    try {
      parsed = JSON.parse(block[1]);
    } catch {
      continue;
    }
    if (parsed?.name !== 'BIM Change Detection') continue;
    const compare = parsed.nodes.find((node) => node.name === 'Compare with Previous');
    const update = parsed.nodes.find((node) => node.name === 'Update Database');
    if (!compare || !update) {
      throw new Error('BIM Change Detection workflow is missing Compare or Update Database');
    }
    return { compare, update };
  }
  throw new Error('BIM Change Detection workflow JSON was not found in SKILL.md');
}

const workflow = loadWorkflow();

/**
 * Run the skill's comparison snippet. load_previous_version is not defined in
 * the template; the snippet indexes the return value by element id, so the
 * stub returns that dictionary. items[0].json.output is the JSON the Extract
 * Model Data command is supposed to print.
 */
function compareElements(currentElements, previousById) {
  const code = workflow.compare.parameters.code;
  assert.equal(typeof code, 'string');
  // The snippet reads items[0].json.output, so the input object has those attributes.
  const program = `
import json
class _Output:
    def __init__(self, output):
        self.output = output
class _Item:
    def __init__(self, output):
        self.json = _Output(output)
def load_previous_version():
    return json.loads(${JSON.stringify(JSON.stringify(previousById))})
items = [_Item(${JSON.stringify(JSON.stringify(currentElements))})]
def _user_function():
${code
  .split('\n')
  .map((line) => `    ${line}`)
  .join('\n')}
result = _user_function()
print(json.dumps(result))
`;
  const run = spawnSync('python3', ['-c', program], { encoding: 'utf8' });
  assert.equal(run.status, 0, run.stderr || run.stdout);
  const returned = JSON.parse(run.stdout);
  assert.ok(Array.isArray(returned) && returned[0]?.json, 'comparison did not return an item');
  return returned[0].json;
}

function getResolvables(expression) {
  if (!expression) return [];
  const resolvables = [];
  const resolvableRegex = /({{[\s\S]*?}})/g;
  let match;
  while ((match = resolvableRegex.exec(expression)) !== null) {
    if (match[1]) resolvables.push(match[1]);
  }
  return resolvables;
}

function stringToArray(str) {
  if (str === undefined) return [];
  return String(str)
    .split(',')
    .filter((entry) => entry)
    .map((entry) => entry.trim());
}

function stringifyParameter(expression) {
  if (expression === undefined) return '';
  if (expression === null) return 'null';
  return typeof expression === 'object' ? JSON.stringify(expression) : expression.toString();
}

/**
 * Same substitution the Postgres executeQuery operation performs.
 * nodeVersion follows the workflow node. An omitted typeVersion stays
 * undefined, which takes the pre-2.5 queryReplacement branch.
 */
function buildExecutableQuery(changes) {
  const parameters = workflow.update.parameters;
  const nodeVersion = workflow.update.typeVersion;
  const tournament = new Tournament(() => {});
  const base = {};
  const data = new Proxy(base, {
    has: () => true,
    get(target, name, receiver) {
      if (name === '$json') return changes;
      return Reflect.get(target, name, receiver);
    },
  });
  // expression.ts initializeGlobalContext allowlists JSON on the data proxy.
  data.JSON = JSON;

  const evaluate = (expression) => tournament.execute(expression, data);

  let query = parameters.query;
  for (const resolvable of getResolvables(query)) {
    query = query.replace(resolvable, evaluate(resolvable));
  }

  let values = [];
  const rawReplacements = parameters.options?.queryReplacement;
  if (typeof rawReplacements === 'string' && rawReplacements) {
    if (nodeVersion >= 2.5) {
      const rawValues = rawReplacements.replace(/^=+/, '');
      const resolvables = getResolvables(rawValues);
      if (resolvables.length) {
        for (const resolvable of resolvables) {
          const rawEvaluated = evaluate(resolvable);
          if (Array.isArray(rawEvaluated)) {
            for (const item of rawEvaluated) {
              if (item === undefined) {
                if (nodeVersion >= 2.8) values.push(null);
                continue;
              }
              values.push(typeof item === 'object' && item !== null ? JSON.stringify(item) : item);
            }
            continue;
          }
          if (nodeVersion >= 2.8) {
            values.push(
              rawEvaluated === null || rawEvaluated === undefined
                ? null
                : stringifyParameter(rawEvaluated),
            );
            continue;
          }
          const evaluatedExpression = stringifyParameter(rawEvaluated);
          const evaluatedValues = (() => {
            try {
              JSON.parse(evaluatedExpression.trim());
              return [evaluatedExpression];
            } catch {
              return stringToArray(evaluatedExpression);
            }
          })();
          if (evaluatedValues.length) values.push(...evaluatedValues);
        }
      } else {
        values.push(...stringToArray(rawValues));
      }
    } else {
      const rawValues = rawReplacements
        .replace(/^=+/, '')
        .split(',')
        .filter((entry) => entry)
        .map((entry) => entry.trim());
      for (const rawValue of rawValues) {
        const resolvables = getResolvables(rawValue);
        if (resolvables.length) {
          for (const resolvable of resolvables) values.push(evaluate(resolvable));
        } else {
          values.push(rawValue);
        }
      }
    }
  } else if (Array.isArray(rawReplacements)) {
    values = rawReplacements;
  }

  if (!rawReplacements) {
    let nextValueIndex = values.length + 1;
    const literals = query.match(/'\$[0-9]+'/g) ?? [];
    for (const literal of literals) {
      query = query.replace(literal, `$${nextValueIndex}`);
      values.push(literal.replace(/'/g, ''));
      nextValueIndex += 1;
    }
  }

  return { query, values };
}

const pgp = pgPromise();
const db = pgp({
  host: process.env.PGHOST || '/var/run/postgresql',
  database: process.env.PGDATABASE || 'postgres',
  user: process.env.PGUSER || process.env.USER,
  password: process.env.PGPASSWORD,
});

async function resetTables() {
  await db.none('DROP TABLE IF EXISTS model_changes');
  await db.none('DROP TABLE IF EXISTS bim_projects');
  await db.none('CREATE TABLE model_changes (timestamp timestamptz, changes text)');
  await db.none('CREATE TABLE bim_projects (id int primary key, name text)');
  await db.none("INSERT INTO bim_projects (id, name) VALUES (1, 'tower-a')");
}

async function runUpdate(changes) {
  const executable = buildExecutableQuery(changes);
  const formatted = pgp.helpers.concat([
    { query: executable.query, values: executable.values, options: { partial: true } },
  ]);
  await db.multi(formatted);
}

async function assertLoggedAsData(changes) {
  const projects = await db.any('SELECT id, name FROM bim_projects ORDER BY id');
  assert.deepEqual(projects, [{ id: 1, name: 'tower-a' }]);
  const rows = await db.any('SELECT changes FROM model_changes');
  assert.equal(rows.length, 1);
  assert.deepEqual(JSON.parse(rows[0].changes), changes);
}

describe('BIM change logging', { concurrency: 1 }, () => {
test('added element name is stored as data', async () => {
  const current = [{ id: 'e1', Name: DROP_NAME }];
  const changes = compareElements(current, {});
  assert.deepEqual(changes.added, current);
  await resetTables();
  await runUpdate(changes);
  await assertLoggedAsData(changes);
});

test('added element property value is stored as data', async () => {
  const current = [{ id: 'e2', Name: 'Wall', properties: { FireRating: DROP_NAME } }];
  const changes = compareElements(current, {});
  assert.deepEqual(changes.added, current);
  await resetTables();
  await runUpdate(changes);
  await assertLoggedAsData(changes);
});

test('modified element name is stored as data', async () => {
  const current = [{ id: 'e3', Name: DROP_NAME }];
  const previous = { e3: { id: 'e3', Name: 'old' } };
  const changes = compareElements(current, previous);
  assert.deepEqual(changes.modified, current);
  await resetTables();
  await runUpdate(changes);
  await assertLoggedAsData(changes);
});

test('deleted element name is stored as data', async () => {
  const previous = { e4: { id: 'e4', Name: DROP_NAME } };
  const changes = compareElements([], previous);
  assert.deepEqual(changes.deleted, [previous.e4]);
  await resetTables();
  await runUpdate(changes);
  await assertLoggedAsData(changes);
});

test('a quote in an element name is stored as data', async () => {
  const current = [{ id: 'e5', Name: QUOTE_NAME }];
  const changes = compareElements(current, {});
  await resetTables();
  await runUpdate(changes);
  await assertLoggedAsData(changes);
});

test('element text cannot splice a query into the insert', async () => {
  const current = [{ id: 'e6', Name: SUBQUERY_NAME }];
  const changes = compareElements(current, {});
  await resetTables();
  await runUpdate(changes);
  await assertLoggedAsData(changes);
});

after(async () => {
  await pgp.end();
});
});
