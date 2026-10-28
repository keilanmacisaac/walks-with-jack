const datePicker = document.getElementById('date-picker');
const formDate = document.getElementById('form-date');
const formTime = document.getElementById('form-time');
const slotGrid = document.getElementById('slot-grid');
const slotCount = document.getElementById('slot-count');
const summary = document.getElementById('selection-summary');
const bookButton = document.getElementById('book-button');

let selectedTime = '';

function prettyDate(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Intl.DateTimeFormat('en-CA', {
    weekday: 'short', month: 'short', day: 'numeric'
  }).format(new Date(y, m - 1, d));
}

function selectSlot(button) {
  document.querySelectorAll('.slot').forEach(el => el.classList.remove('selected'));
  button.classList.add('selected');
  selectedTime = button.dataset.value;
  formTime.value = selectedTime;
  summary.innerHTML = `<strong>${prettyDate(formDate.value)}</strong><span>${button.textContent.trim()} for 5 minutes</span>`;
  bookButton.disabled = false;
}

function wireSlots() {
  document.querySelectorAll('.slot').forEach(button => {
    button.addEventListener('click', () => selectSlot(button));
  });
}

async function loadSlots(date) {
  selectedTime = '';
  formTime.value = '';
  formDate.value = date;
  bookButton.disabled = true;
  summary.innerHTML = '<span>No time selected yet</span>';
  slotGrid.innerHTML = '<div class="empty-slots">Loading available times…</div>';

  const response = await fetch(`/api/slots?date=${encodeURIComponent(date)}`);
  const data = await response.json();
  if (!response.ok) {
    slotGrid.innerHTML = `<div class="empty-slots">${data.error || 'Could not load times.'}</div>`;
    slotCount.textContent = '0 open';
    return;
  }

  slotCount.textContent = `${data.slots.length} open`;
  if (!data.slots.length) {
    slotGrid.innerHTML = '<div class="empty-slots">No open times on this day. Try another date.</div>';
    return;
  }

  slotGrid.innerHTML = data.slots.map(slot =>
    `<button type="button" class="slot" data-value="${slot.value}">${slot.label}</button>`
  ).join('');
  wireSlots();
}

datePicker?.addEventListener('change', () => loadSlots(datePicker.value));
wireSlots();
