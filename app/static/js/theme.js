(function () {
  function resolve(theme) {
    if (theme === "system") {
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    return theme;
  }

  function markActive(theme) {
    document.querySelectorAll(".theme-btn").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.theme === theme);
    });
  }

  var current = document.documentElement.getAttribute("data-user-theme") || "system";
  markActive(current);

  document.querySelectorAll(".theme-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var theme = btn.dataset.theme;
      document.documentElement.setAttribute("data-bs-theme", resolve(theme));
      document.documentElement.setAttribute("data-user-theme", theme);
      markActive(theme);
      fetch("/settings/theme", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": window.CSRF_TOKEN || "",
        },
        body: JSON.stringify({ theme: theme }),
      }).catch(function () {});
    });
  });
})();
