const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const moduleUnderTest = { exports: {} }
vm.runInNewContext(ts.transpileModule(fs.readFileSync(
  path.join(__dirname, '../src/lib/manualOrder.ts'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText, { exports: moduleUnderTest.exports, module: moduleUnderTest, fetch() {} })
const { ManualOrderClient } = moduleUnderTest.exports
const RID = '1e0dc21c-999e-4f1b-9f9d-a1d601c84d76'
const payload = { ticker: 'AAPL', qty: 1, side: 'buy', order_type: 'market' }
const response = (status, body) => ({ status, json: async () => body })
function storage() {
  const data = new Map()
  return { getItem: k => data.get(k) ?? null, setItem: (k, v) => data.set(k, v), removeItem: k => data.delete(k),
    key: i => Array.from(data.keys())[i] ?? null, get length() { return data.size } }
}

test('timeout persists identity; reload reconciles instead of placing another order', async () => {
  const saved = storage(), calls = []
  const first = new ManualOrderClient('http://local', saved, async (url, req) => {
    calls.push({ url, req }); throw new Error('timeout after acceptance')
  }, () => RID)
  assert.equal((await first.submit(payload)).status, 'pending')
  assert.equal(first.pending, true)
  const restored = new ManualOrderClient('http://local', saved, async (url, req) => {
    calls.push({ url, req }); return response(200, { status: 'confirmed', id: 'accepted' })
  }, () => { throw new Error('must preserve the old identity') })
  assert.equal((await restored.submit({ ...payload, qty: 2 })).id, 'accepted')
  assert.equal(calls.length, 2)
  assert.equal(calls[0].req.method, 'POST')
  assert.equal(JSON.parse(calls[0].req.body).request_id, RID)
  assert.equal(calls[1].req, undefined)
  assert.match(calls[1].url, new RegExp(RID))
  assert.equal(restored.pending, false)
})

test('an unrecorded request retries with the original identity', async () => {
  const saved = storage(), ids = [], calls = []
  const client = new ManualOrderClient('http://local', saved, async (url, req) => {
    calls.push(url)
    if (req) {
      ids.push(JSON.parse(req.body).request_id)
      if (ids.length === 1) throw new Error('request never reached server')
      return response(200, { status: 'confirmed', id: 'one' })
    }
    return response(404, {})
  }, () => RID)
  await client.submit(payload)
  assert.equal((await client.reconcile()).id, 'one')
  assert.deepEqual(ids, [RID, RID])
})

test('unknown, conflict and unavailable states retain the request', async () => {
  for (const status of [202, 409, 503]) {
    const client = new ManualOrderClient('http://local', storage(), async () =>
      response(status, { detail: 'must reconcile' }), () => RID)
    assert.equal((await client.submit(payload)).status, 'pending')
    assert.equal(client.pending, true)
  }
})

test('definitive rejection releases the request; storage failure never submits', async () => {
  const saved = storage()
  const rejected = new ManualOrderClient('http://local', saved, async () =>
    response(400, { status: 'rejected', error: 'insufficient funds' }), () => RID)
  assert.equal((await rejected.submit(payload)).status, 'rejected')
  assert.equal(rejected.pending, false)
  let posts = 0
  const broken = new ManualOrderClient('http://local', { ...saved, setItem() { throw new Error('storage unavailable') } },
    async () => { posts++; return response(200, {}) }, () => RID)
  await assert.rejects(broken.submit(payload), /storage unavailable/)
  assert.equal(posts, 0)
})

test('parallel checks never generate a second request identity', async () => {
  let release, calls = 0
  const client = new ManualOrderClient('http://local', storage(), async () => {
    calls++; await new Promise(resolve => { release = resolve })
    return response(202, { status: 'pending' })
  }, () => RID)
  const first = client.submit(payload)
  assert.equal((await client.submit(payload)).status, 'pending')
  release()
  await first
  assert.equal(calls, 1)
})

test('a second tab reconciles a shared pending request; multiple identities survive reload', async () => {
  const saved = storage(), calls = []
  const request = async (url, req) => {
    calls.push({ url, req }); return response(202, { status: 'pending' })
  }
  const first = new ManualOrderClient('http://local', saved, request, () => RID)
  const second = new ManualOrderClient('http://local', saved, request, () => { throw new Error('no new identity') })
  await first.submit(payload)
  await second.submit({ ...payload, qty: 2 })
  assert.equal(calls.length, 2)
  assert.equal(calls[1].req, undefined)
  // If two tabs create requests simultaneously, retain both durable records.
  const another = '2e0dc21c-999e-4f1b-9f9d-a1d601c84d76'
  saved.setItem('stonkmonitor.manualOrder.' + another, JSON.stringify({ request_id: another, payload }))
  const restored = new ManualOrderClient('http://local', saved, async () =>
    response(200, { status: 'confirmed', id: 'resolved' }))
  assert.equal((await restored.reconcile()).status, 'confirmed')
  assert.equal(restored.pending, true)
  assert.equal((await restored.reconcile()).status, 'confirmed')
  assert.equal(restored.pending, false)
  assert.equal(saved.length, 0)
})

test('legacy pending identity is retained; malformed storage prevents submission', async () => {
  const saved = storage()
  saved.setItem('stonkmonitor.manualOrder', JSON.stringify({ request_id: RID, payload }))
  const restored = new ManualOrderClient('http://local', saved, async url => {
    assert.match(url, new RegExp(RID)); return response(202, { status: 'pending' })
  })
  assert.equal((await restored.reconcile()).status, 'pending')
  assert.equal(saved.getItem('stonkmonitor.manualOrder'), null)
  assert.ok(saved.getItem('stonkmonitor.manualOrder.' + RID))
  for (const raw of ['null', '{"request_id":"bad"}', '{broken']) {
    const broken = storage()
    broken.setItem('stonkmonitor.manualOrder', raw)
    assert.throws(() => new ManualOrderClient('http://local', broken))
  }
})
