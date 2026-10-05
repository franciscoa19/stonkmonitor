const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const { createElement } = require('react')
const { renderToStaticMarkup } = require('react-dom/server')

const SRC = path.join(__dirname, '../src')

// Transpile a source file (and its `@/` imports) and evaluate it. No bundler,
// no network: fetch throws if a render ever tries to use it.
function load(file, cache = new Map()) {
  if (cache.has(file)) return cache.get(file).exports
  const { outputText } = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  })
  const module = { exports: {} }
  cache.set(file, module)
  vm.runInNewContext(outputText, {
    module,
    exports: module.exports,
    process: { env: {} },
    fetch: () => { throw new Error('no network in tests') },
    require: id => {
      if (!id.startsWith('@/')) return require(id)
      const base = path.join(SRC, id.slice(2))
      return load(['.ts', '.tsx'].map(ext => base + ext).find(candidate => fs.existsSync(candidate)), cache)
    },
  })
  return module.exports
}

const { isAccount } = load(path.join(SRC, 'lib/account.ts'))
const { TradePanel } = load(path.join(SRC, 'components/TradePanel.tsx'))
const GOOD = { equity: 52156.97, cash: 52156.97, buying_power: 208627.88, day_trade_count: 0, status: 'ACTIVE' }
const render = props => renderToStaticMarkup(
  createElement(TradePanel, { positions: [], onRefresh() {}, ...props }))

test('isAccount accepts only a payload with finite numeric balances', () => {
  assert.equal(isAccount(GOOD), true)
  for (const bad of [null, undefined, {}, [], 'ACTIVE', { ...GOOD, equity: '52156.97' },
    { ...GOOD, cash: NaN }, { equity: 1, cash: 1 }]) {
    assert.equal(isAccount(bad), false, JSON.stringify(bad))
  }
})

test('the Trade panel renders an unusable account without crashing', () => {
  // `{}` is what /api/account used to return on a broker failure. It is truthy,
  // so the panel called account.equity.toLocaleString() and threw.
  for (const account of [{}, null, { equity: 1 }]) {
    const html = render({ account, accountStale: true })
    assert.match(html, /Broker account unavailable/)
    assert.doesNotMatch(html, /Buying Power/)
  }
  assert.doesNotMatch(render({ account: {} }), /Buying Power/)
})

test('a stale account keeps its last values and says so', () => {
  const html = render({ account: GOOD, accountStale: true })
  assert.match(html, /last known account values/)
  assert.match(html, /Buying Power/)
  assert.match(html, /52,156\.97/)
})

test('a healthy account shows no warning', () => {
  const html = render({ account: GOOD })
  assert.match(html, /Equity/)
  assert.doesNotMatch(html, /Broker/)
})
