const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')

test('disposing the hook cancels timers and prevents a reconnect', () => {
  let cleanup
  const timeouts = new Set()
  const intervals = new Set()
  const sockets = []
  class FakeSocket {
    static OPEN = 1
    readyState = 1
    constructor() { sockets.push(this) }
    send() {}
    close() { this.onclose?.() }
  }
  const source = fs.readFileSync(path.join(__dirname, '../src/lib/useWebSocket.ts'), 'utf8')
  const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } })
  const exports = {}
  vm.runInNewContext(outputText, {
    exports, WebSocket: FakeSocket,
    require: () => ({
      useRef: value => ({ current: value }),
      useState: value => [value, () => {}],
      useEffect: effect => { cleanup = effect() },
    }),
    setTimeout: fn => { timeouts.add(fn); return fn },
    clearTimeout: id => timeouts.delete(id),
    setInterval: fn => { intervals.add(fn); return fn },
    clearInterval: id => intervals.delete(id),
  })
  exports.useWebSocket('ws://localhost:8000/ws')
  const socket = sockets[0]
  socket.onopen()
  assert.equal(intervals.size, 1)
  const delayedClose = socket.onclose
  cleanup()
  delayedClose() // a close event already queued before unmount
  assert.equal(timeouts.size, 0)
  assert.equal(intervals.size, 0)
  assert.equal(sockets.length, 1)
})
