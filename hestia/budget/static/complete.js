/* Same-origin bridge: neither the callback code nor its URL reaches this document. */
'use strict';
const message = document.getElementById('message');
const flows = document.getElementById('flows');
let busy = false;
const ready = new Set(['staged', 'candidate', 'exchanging']);
const terminal = new Map([
  ['canceled', 'Połączenie zostało anulowane. Wróć do budżetu, aby rozpocząć ponownie.'],
  ['expired', 'Połączenie wygasło. Wróć do budżetu, aby rozpocząć ponownie.'],
  ['failed', 'Nie ukończono autoryzacji w banku. Wróć do budżetu, aby rozpocząć ponownie.'],
]);
class BridgeError extends Error {}
async function request(operation, body) {
  const response = await fetch('/api/budget/consent/' + operation, {
    method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (response.status === 401) throw new BridgeError('Zaloguj się ponownie do Hestii i rozpocznij nowe połączenie z bankiem.');
  if (!response.ok) throw new BridgeError('Nie udało się zakończyć połączenia. Wróć do budżetu i sprawdź jego stan.');
  return response.json();
}
function showError(error) {
  message.textContent = error instanceof BridgeError ? error.message
    : 'Nie udało się połączyć z Hestią. Wróć do budżetu i spróbuj ponownie.';
}
async function complete(flow) {
  if (busy) return;
  busy = true;
  flows.replaceChildren();
  message.textContent = 'Weryfikuję zgodę bankową…';
  try {
    const result = await request('complete', { flow_id: flow.id });
    if (result.status === 'adopted') {
      message.textContent = 'Bank został połączony. Wybrane konta i historia zostały zachowane. Możesz wrócić do Hestii.';
    } else if (result.status === 'exchange_uncertain') {
      message.textContent = 'Bank nie potwierdził wyniku wymiany kodu. Hestia nie powtórzy tej operacji. Sprawdź stan połączenia w budżecie.';
    } else if (result.status === 'reconciliation_required' || result.status === 'in_progress') {
      message.textContent = 'Zgoda czeka na dokończenie weryfikacji. Możesz spróbować ponownie; Hestia nie wymieni kodu drugi raz.';
      addButton(flow, 'Dokończ weryfikację');
    } else {
      message.textContent = terminal.get(result.status) ?? 'Nie udało się zakończyć połączenia. Sprawdź jego stan w budżecie Hestii.';
    }
  } catch (error) { showError(error); }
  finally { busy = false; }
}
function addButton(flow, label) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = label;
  button.addEventListener('click', () => { void complete(flow); });
  flows.append(button);
}
async function begin() {
  try {
    const result = await request('pending');
    const candidates = result.pending.filter(flow => ready.has(flow.status));
    if (candidates.length === 1) { await complete(candidates[0]); return; }
    message.textContent = candidates.length
      ? 'Wybierz połączenie, które chcesz dokończyć.'
      : 'Brak oczekującego połączenia w tej sesji przeglądarki. Użyj tej samej przeglądarki, w której rozpoczęto połączenie. Jeśli bank odmówił zgody lub anulowano autoryzację, wróć do budżetu i rozpocznij ponownie.';
    for (const flow of candidates) addButton(flow, 'Dokończ: ' + flow.connection_id);
  } catch (error) { showError(error); }
}
void begin();
