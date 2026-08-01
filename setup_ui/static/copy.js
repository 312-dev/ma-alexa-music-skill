// Copy buttons for values destined for another site's forms.
//
// The Clipboard API only exists on secure origins, and this panel commonly
// runs over plain http inside a private network, so the select-and-execCommand
// fallback is a primary path here, not a legacy nicety. Without JavaScript the
// rows degrade to readonly inputs that can still be selected and copied by
// hand.
document.addEventListener("click", function (event) {
  var button = event.target.closest("[data-copy]");
  if (!button) return;
  var input = document.getElementById(button.getAttribute("data-copy"));
  if (!input) return;
  input.focus();
  input.select();
  input.setSelectionRange(0, input.value.length);
  var done = function () {
    button.textContent = "Copied";
    setTimeout(function () { button.textContent = "Copy"; }, 1600);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(input.value).then(done);
  } else if (document.execCommand("copy")) {
    done();
  }
});
