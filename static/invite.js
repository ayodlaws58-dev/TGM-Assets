document.addEventListener("DOMContentLoaded", function () {
  var params = new URLSearchParams(window.location.hash.slice(1));
  var token = params.get("access_token") || "";
  var type = params.get("type") || "";
  window.history.replaceState(null, "", window.location.pathname);
  if (token && type === "invite") {
    document.getElementById("access-token").value = token;
    document.getElementById("invite-type").value = type;
    document.getElementById("invite-submit").disabled = false;
    document.getElementById("invite-error").textContent = "";
  } else {
    document.getElementById("invite-error").textContent =
      "This invitation link is invalid or expired. Ask an administrator to send another invitation.";
  }
});
