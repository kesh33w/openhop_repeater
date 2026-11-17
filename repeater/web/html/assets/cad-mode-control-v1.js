// Isolated CAD mode control, shared by source builds and the deployed static UI.
// No radio setting is changed until the operator presses Save CAD mode.
let mountedSelect = null;
let mountedValue = null;
let refresh = () => {};
let cleanup = () => {};
function mount(select) {
  cleanup();
  mountedSelect = select;
  if (!select) return;
  const panel = document.createElement('section');
  panel.id = 'cad-mode-control';
  panel.className = 'mt-4 space-y-2';
  panel.setAttribute('aria-label', 'CAD transmit mode');
  panel.innerHTML = `<label style="display:flex;align-items:center;gap:10px;font-weight:600"><input type="checkbox" id="cad-before-transmit" disabled> CAD before transmit</label><p style="font-size:13px">Off skips channel-activity checks before transmitting; collisions are more likely.</p><p data-state style="font-size:13px"></p><button type="button" class="btn-primary" disabled>Save CAD mode</button><p role="status" aria-live="polite" style="font-size:13px"></p>`;
  select.parentElement.append(panel);
  const checkbox = panel.querySelector('input');
  const button = panel.querySelector('button');
  const state = panel.querySelector('[data-state]');
  const status = panel.querySelector('[role=status]');
  let generation = 0, saving = false, available = false, alive = true, acknowledged = false;
  function controls() { checkbox.disabled = button.disabled = saving || !available || select.disabled; }
  async function request(path, data) {
    const token = localStorage.getItem('pymc_jwt_token');
    if (!token) throw new Error('Sign in again before changing CAD mode.');
    const response = await fetch('/api/' + path, {
      method: data ? 'POST' : 'GET',
      headers: {Authorization: 'Bearer ' + token, ...(data ? {'Content-Type':'application/json'} : {})},
      ...(data ? {body: JSON.stringify(data)} : {}),
    });
    const body = await response.json();
    if (!response.ok || body.success === false) throw new Error(body.error || body.message || 'CAD mode request failed.');
    return body.data || body;
  }
  async function load() {
    const version = ++generation, id = select.value;
    mountedValue = id;
    available = false; controls();
    if (!id) {state.textContent = 'Select a radio to view its CAD transmit setting.'; return;}
    state.textContent = 'Loading CAD transmit setting…'; status.textContent = '';
    try {
      const result = await request('cad_radios');
      if (!alive || version !== generation || id !== select.value) return;
      const radio = result.radios?.find(r => r.radio_id === id);
      if (!radio || typeof radio.cad_enabled !== 'boolean') throw new Error('CAD transmit setting is unavailable for this radio.');
      checkbox.checked = acknowledged = radio.cad_enabled;
      available = radio.supports_cad_toggle === true;
      state.textContent = !available ? 'This radio does not support changing CAD mode.' : radio.cad_mode_explicit ? `Current mode: ${radio.cad_enabled ? 'On' : 'Off'}.` : `Current host setting: ${radio.cad_enabled ? 'On' : 'Off'} (existing setting). Firmware mode has not been synchronized; the first save synchronizes it.`;
    } catch (error) { if (alive && version === generation) state.textContent = error.message; }
    finally { if (alive && version === generation) controls(); }
  }
  button.addEventListener('click', async () => {
    if (saving || !available || select.disabled) return;
    const id = select.value, enabled = checkbox.checked;
    saving = true; controls(); status.textContent = 'Saving CAD mode…';
    try {
      const result = await request('set_cad_enabled', {radio_id:id, enabled});
      if (!alive || select.value !== id) return;
      if (result.settings && (result.settings.radio_id !== id || result.settings.enabled !== enabled)) throw new Error('Radio returned a different CAD mode; reload to verify.');
      acknowledged = enabled;
      state.textContent = `Current mode: ${enabled ? 'On' : 'Off'}.`;
      status.textContent = result.message || 'CAD mode saved.';
    } catch (error) { if (alive && select.value === id) {checkbox.checked = acknowledged; status.textContent = `Not saved: ${error.message}`;} }
    finally {saving = false; if (alive) controls();}
  });
  select.addEventListener('change', load);
  const observer = new MutationObserver(controls);
  observer.observe(select, {attributes:true,attributeFilter:['disabled']});
  cleanup = () => {alive=false;generation++;observer.disconnect();select.removeEventListener('change',load);panel.remove();};
  refresh = load;
  void load();
}
let scheduled = false;
function schedule() {
  if (scheduled) return;
  scheduled = true;
  queueMicrotask(() => {
    scheduled = false;
    const select = document.querySelector('#cad-radio');
    if (select !== mountedSelect) mount(select);
    else if (select && select.value !== mountedValue) void refresh();
  });
}
new MutationObserver(schedule).observe(document.body, {childList:true,subtree:true});
schedule();
