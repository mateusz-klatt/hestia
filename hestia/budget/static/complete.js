/* Same-origin bridge: neither the callback code nor its URL reaches this document. */
'use strict';
const message = document.getElementById('message');
const flows = document.getElementById('flows');
let busy = false;
const ready = new Set(['staged', 'candidate', 'exchanging']);
async function request(operation, body) {
  const response = await fetch('/api/budget/consent/' + operation, {
    method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (response.status === 401) throw new Error('Zaloguj się ponownie do Hestii i rozpocznij nowe połączenie z bankiem.');
  if (!response.ok) throw new Error('Nie udało się zakończyć połączenia. Wróć do budżetu i sprawdź jego stan.');
  return response.json();
}
function showError(error) { message.textContent = error.message; }
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
    } else {
      message.textContent = 'Zgoda czeka na dokończenie weryfikacji. Możesz spróbować ponownie; Hestia nie wymieni kodu drugi raz.';
      addButton(flow, 'Dokończ weryfikację');
    }
  } catch (error) { showError(error); }
  finally { busy = false; }
}
function addButton(flow, label) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = label;
  button.addEventListener('click', () => complete(flow));
  flows.append(button);
}
async function begin() {
  try {
    const result = await request('pending');
    const candidates = result.pending.filter(flow => ready.has(flow.status));
    if (candidates.length === 1) { await complete(candidates[0]); return; }
    message.textContent = candidates.length
      ? 'Wybierz połączenie, które chcesz dokończyć.'
      : 'Brak oczekującego połączenia w tej sesji przeglądarki. Sprawdź stan w budżecie Hestii.';
    for (const flow of candidates) addButton(flow, 'Dokończ: ' + flow.connection_id);
  } catch (error) { showError(error); }
}
begin();
