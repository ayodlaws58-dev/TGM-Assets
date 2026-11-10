document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) e.preventDefault();
});

document.addEventListener("click", function (e) {
  if (e.target.closest(".print-label")) {
    window.print();
    return;
  }
  var button = e.target.closest(".decision-form button[name='decision']");
  if (!button) return;
  var note = button.form.querySelector("[name='note']");
  if (note) note.required = button.value === "Rejected";
});

document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("select[name='schedule_id']").forEach(function (scheduleSelect) {
    var form = scheduleSelect.closest("form");
    var assetSelect = form && form.querySelector("select[name='asset_id']");
    if (!assetSelect) return;
    function filterSchedules() {
      Array.from(scheduleSelect.options).forEach(function (option) {
        var matches = !option.dataset.assetId || option.dataset.assetId === assetSelect.value;
        option.hidden = !matches;
        if (!matches && option.selected) scheduleSelect.value = "";
      });
    }
    assetSelect.addEventListener("change", filterSchedules);
    filterSchedules();
  });
  var toggle = document.querySelector(".nav-toggle");
  var nav = document.getElementById("primary-nav");
  if (!toggle || !nav) return;
  toggle.addEventListener("click", function () {
    var expanded = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", String(!expanded));
    nav.classList.toggle("is-open", !expanded);
  });
});
