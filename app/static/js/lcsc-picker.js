// LCSC product-photo picker.
//
// Used twice — once on a part's edit page (searching that part's own MPN)
// and once in bulk edit (searching a keyword, then linking the result to
// every selected part). The two used to carry a verbatim copy each of the
// search/render logic, differing only in element ids and the endpoint.
//
// Markup contract: one container per picker,
//
//   <div class="lcsc-picker" data-search-url="/parts/…/lcsc_search">
//     <input  data-lcsc="keyword">      the MPN/keyword box
//     <button data-lcsc="search">       runs the lookup
//     <div    data-lcsc="result">       status line
//     <div    data-lcsc="suggestions">  candidate cards, when no exact hit
//     <div    data-lcsc="choices">      the chosen listing's images
//     <button data-lcsc="download">     submits the enclosing form
//   </div>
//
// Everything below is wired from those, so a page only supplies markup.
(function () {
  document.querySelectorAll(".lcsc-picker").forEach(init);

  function init(root) {
    var searchUrl = root.dataset.searchUrl;
    var keywordInput = root.querySelector('[data-lcsc="keyword"]');
    var searchBtn = root.querySelector('[data-lcsc="search"]');
    var resultDiv = root.querySelector('[data-lcsc="result"]');
    var suggestionsDiv = root.querySelector('[data-lcsc="suggestions"]');
    var choicesDiv = root.querySelector('[data-lcsc="choices"]');
    var downloadBtn = root.querySelector('[data-lcsc="download"]');
    if (!searchUrl || !keywordInput || !searchBtn) return;

    function say(text, danger) {
      resultDiv.textContent = text;
      resultDiv.className = "small mb-2 " + (danger ? "text-danger" : "text-secondary");
    }

    // The first two images are pre-selected: LCSC's first shots are usually
    // the product itself, later ones are packaging/reel photos.
    function renderImageChoices(images) {
      choicesDiv.innerHTML = "";
      images.forEach(function (url, i) {
        var wrap = document.createElement("label");
        wrap.className = "mb-0";
        wrap.innerHTML =
          '<input type="checkbox" name="image_url" value="' + url + '" class="d-none lcsc-check"' + (i < 2 ? " checked" : "") + ">" +
          '<img src="' + url + '" class="photo-thumb lcsc-thumb' + (i < 2 ? " active" : "") + '" alt="">';
        choicesDiv.appendChild(wrap);
      });
      downloadBtn.classList.remove("d-none");
    }

    function describe(s) {
      return "Selected: " + s.product_model + " (" + s.product_code + ")" +
        (s.brand ? " — " + s.brand : "") + (s.encap ? " · " + s.encap : "") +
        ". Click image(s) to select, then Download.";
    }

    function renderSuggestions(suggestions) {
      suggestionsDiv.innerHTML = "";
      suggestions.forEach(function (s) {
        var card = document.createElement("div");
        card.className = "lcsc-suggestion text-center";
        card.innerHTML =
          '<img src="' + s.images[0] + '" class="photo-thumb lcsc-thumb" alt="">' +
          '<div class="small text-secondary" style="overflow-wrap:anywhere;">' + s.product_model +
          (s.brand ? "<br>" + s.brand : "") + "</div>";
        card.addEventListener("click", function () {
          Array.prototype.forEach.call(suggestionsDiv.children, function (c) {
            c.classList.remove("selected");
          });
          card.classList.add("selected");
          say(describe(s));
          renderImageChoices(s.images);
        });
        suggestionsDiv.appendChild(card);
      });
    }

    function doSearch() {
      var kw = keywordInput.value.trim();
      if (!kw) return;
      say("Searching…");
      suggestionsDiv.innerHTML = "";
      choicesDiv.innerHTML = "";
      downloadBtn.classList.add("d-none");

      fetch(searchUrl + "?keyword=" + encodeURIComponent(kw))
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.found && data.exact_matches) {
            say("Multiple exact matches on LCSC for that keyword (different manufacturers/listings) — pick the right one:");
            renderSuggestions(data.exact_matches);
          } else if (data.found) {
            say("Matched: " + data.product_model + " (" + data.product_code + ")" +
              (data.brand ? " — " + data.brand : "") + (data.encap ? " · " + data.encap : "") +
              ". Click image(s) to select, then Download.");
            renderImageChoices(data.images);
          } else if (data.suggestions && data.suggestions.length) {
            say("No exact match, but here are similar parts on LCSC — pick the right one:");
            renderSuggestions(data.suggestions);
          } else {
            say(data.error || "No match found on LCSC for that keyword. Try adjusting it (remove a suffix, fix a typo).", true);
          }
        })
        .catch(function () {
          say("Lookup failed, try again.", true);
        });
    }

    searchBtn.addEventListener("click", doSearch);
    keywordInput.addEventListener("keydown", function (e) {
      // Enter would otherwise submit the enclosing download form.
      if (e.key === "Enter") {
        e.preventDefault();
        doSearch();
      }
    });

    // The checkbox lives inside a <label> with the thumbnail, so clicking
    // the image already toggles it natively — just mirror that onto the
    // .active class.
    choicesDiv.addEventListener("change", function (e) {
      if (!e.target.classList.contains("lcsc-check")) return;
      var img = e.target.nextElementSibling;
      if (img) img.classList.toggle("active", e.target.checked);
    });
  }
})();
