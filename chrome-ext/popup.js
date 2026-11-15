const $ = (id) => document.getElementById(id)
const store = chrome.storage.session
const STATUS = { off: 'Off', connecting: 'Connecting', connected: 'Connected' }

// sidePanel.open needs the click's user gesture, so the window id is fetched
// up front and the call happens before any await in the handler.
let windowId = null
chrome.windows.getCurrent().then((w) => { windowId = w.id })

function when(ms) {
  return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

async function render() {
  const s = await store.get(['status', 'allow', 'task', 'lastSession'])
  const status = s.status || 'off'
  $('status').textContent = STATUS[status] || status
  $('status').className = `pill ${status}`
  const on = status !== 'off'
  $('pairing').hidden = on
  $('live').hidden = !on
  $('live').style.display = on ? 'flex' : 'none'
  $('allow').textContent = s.allow && s.allow.length ? s.allow.join(', ') : 'None'
  $('taskrow').hidden = !s.task
  $('task').textContent = s.task || ''
  const l = s.lastSession
  $('last').textContent = l ? `${l.steps} steps · ${when(l.start)} to ${when(l.end)}` : 'None'
}

function openPanel() {
  if (windowId !== null) chrome.sidePanel.open({ windowId }).catch(() => {})
}

$('connect').addEventListener('click', async () => {
  openPanel()
  $('error').textContent = ''
  $('connect').disabled = true
  const r = await chrome.runtime.sendMessage({ type: 'pair', code: $('code').value })
  $('connect').disabled = false
  if (!r || !r.ok) $('error').textContent = (r && r.error) || 'Could not connect'
  render()
})
$('code').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('connect').click() })
$('openlog').addEventListener('click', openPanel)
$('disconnect').addEventListener('click', async () => {
  await chrome.runtime.sendMessage({ type: 'disconnect' })
  render()
})
chrome.storage.onChanged.addListener((_, area) => { if (area === 'session') render() })
render()
