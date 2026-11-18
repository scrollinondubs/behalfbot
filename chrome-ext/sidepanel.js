const $ = (id) => document.getElementById(id)
const store = chrome.storage.session
const STATUS = { off: 'Off', connecting: 'Connecting', connected: 'Connected' }

async function render() {
  const s = await store.get(['status', 'task', 'log', 'turn', 'current'])
  const status = s.status || 'off'
  $('status').textContent = STATUS[status] || status
  $('status').className = `pill ${status}`
  $('task').textContent = s.task || (status === 'off' ? 'Not connected. Pair from the toolbar button.' : '')
  $('turn').hidden = !s.turn
  $('turntext').textContent = s.turn ? s.turn.text : ''
  turnNonce = s.turn ? s.turn.nonce : null
  $('disconnect').hidden = status === 'off'
  const ul = $('log')
  ul.textContent = ''
  for (const e of s.log || []) {
    const li = document.createElement('li')
    li.className = e.kind || 'ok'
    li.textContent = e.text
    ul.appendChild(li)
  }
  if (s.current) {
    const li = document.createElement('li')
    li.className = 'now'
    li.textContent = s.current
    ul.appendChild(li)
  }
}

let turnNonce = null

// Done counts only from a real click or key press on this button, never a
// scripted .click(), and names the turn it was shown for.
$('done').addEventListener('click', async (e) => {
  if (!e.isTrusted || !turnNonce) return
  $('done').disabled = true
  await chrome.runtime.sendMessage({ type: 'done', turn: turnNonce }).catch(() => {})
  $('done').disabled = false
})
$('disconnect').addEventListener('click', () => chrome.runtime.sendMessage({ type: 'disconnect' }))
chrome.storage.onChanged.addListener((_, area) => { if (area === 'session') render() })
render()
