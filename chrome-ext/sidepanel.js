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

$('done').addEventListener('click', async () => {
  $('done').disabled = true
  await chrome.runtime.sendMessage({ type: 'done' })
  $('done').disabled = false
})
$('disconnect').addEventListener('click', () => chrome.runtime.sendMessage({ type: 'disconnect' }))
chrome.storage.onChanged.addListener((_, area) => { if (area === 'session') render() })
render()
