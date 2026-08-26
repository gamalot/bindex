// Generic collapsible/sortable tree controller.
//
// Drives both the Locations tree and the Categories tree — they're the same
// shape of data (a shallow admin-editable hierarchy) and used the same way,
// so they run the same code rather than two parallel implementations. What
// differs per page is declared on the markup, not branched on here:
//
//   data-tree-key       storage namespace, so each tree remembers its own
//                       expand/collapse state independently
//   data-reorder-url    POST endpoint for drag-to-reorder/reparent
//   data-editable       "1" to enable dragging
//   data-context-menu   selector for this tree's right-click menu
//
// Menu items declare themselves with data-ctx-action ("add" / "edit" /
// "delete") or data-ctx-href (a link whose {id} placeholder is filled from
// the right-clicked node), so a page can offer extra actions — Categories
// has "View parts" and "Attribute definitions" — without this file knowing
// about them.
(function () {
  document.querySelectorAll(".sortable-tree").forEach(function (rootEl) {
    initCollapse(rootEl);
    initSortable(rootEl);
    initContextMenu(rootEl);
  });

  // ---------------------------------------------------------------------
  // Expand / collapse, persisted across page loads
  // ---------------------------------------------------------------------
  // A plain server re-render always resets to "top level expanded,
  // everything below collapsed", and every create/edit/delete/reorder is a
  // full page reload — so without persistence any manual expand/collapse
  // is lost every single time.
  //
  // Rather than writing an incremental per-toggle diff into storage (easy
  // to get subtly wrong — a node whose own toggle was never clicked keeps
  // whatever the *previous* page load wrote for that id), the whole tree's
  // actual current state (read back off each button's aria-expanded) is
  // snapshotted in one shot right before the page unloads, via pagehide.
  // Self-correcting: whatever is on screen when you navigate away is
  // exactly what comes back.
  function storageKey(rootEl) {
    return "bindex." + (rootEl.dataset.treeKey || "tree") + "TreeExpanded";
  }

  function loadExpandedState(rootEl) {
    try {
      return JSON.parse(localStorage.getItem(storageKey(rootEl)) || "{}");
    } catch (e) {
      return {};
    }
  }

  function isTopLevel(li) {
    return !!(li && li.parentElement && li.parentElement.classList.contains("tree-root"));
  }

  function setExpanded(btn, expanded) {
    var li = btn.closest("li.tree-node-wrap");
    var childUl = li && li.querySelector(":scope > ul.tree-children");
    if (!childUl) return;
    childUl.classList.toggle("d-none", !expanded);
    btn.setAttribute("aria-expanded", String(expanded));
    var icon = btn.querySelector("i");
    if (icon) {
      icon.classList.toggle("bi-chevron-right", !expanded);
      icon.classList.toggle("bi-chevron-down", expanded);
    }
  }

  function initCollapse(rootEl) {
    // Apply saved state on top of the server-rendered default; nodes with
    // no saved entry (brand new, or a first-ever visit) keep that default.
    var saved = loadExpandedState(rootEl);
    rootEl.querySelectorAll(".tree-toggle").forEach(function (btn) {
      var li = btn.closest("li.tree-node-wrap");
      var id = li && li.dataset.id;
      if (id && Object.prototype.hasOwnProperty.call(saved, id)) {
        setExpanded(btn, saved[id]);
      }
    });

    window.addEventListener("pagehide", function () {
      var emptyOnlyCheck = rootEl.querySelector('[data-tree-action="empty-only"]');
      if (emptyOnlyCheck && emptyOnlyCheck.checked) return; // don't save the filter's forced-open view
      var state = {};
      rootEl.querySelectorAll(".tree-toggle").forEach(function (btn) {
        var li = btn.closest("li.tree-node-wrap");
        if (li && li.dataset.id) {
          state[li.dataset.id] = btn.getAttribute("aria-expanded") === "true";
        }
      });
      try {
        localStorage.setItem(storageKey(rootEl), JSON.stringify(state));
      } catch (e) {}
    });

    rootEl.addEventListener("click", function (e) {
      var btn = e.target.closest(".tree-toggle");
      if (!btn || !rootEl.contains(btn)) return;
      e.preventDefault();
      setExpanded(btn, btn.getAttribute("aria-expanded") !== "true");
    });

    var expandAll = rootEl.querySelector('[data-tree-action="expand-all"]');
    var collapseAll = rootEl.querySelector('[data-tree-action="collapse-all"]');
    if (expandAll) {
      expandAll.addEventListener("click", function () {
        rootEl.querySelectorAll(".tree-toggle").forEach(function (btn) {
          setExpanded(btn, true);
        });
      });
    }
    if (collapseAll) {
      collapseAll.addEventListener("click", function () {
        rootEl.querySelectorAll(".tree-toggle").forEach(function (btn) {
          setExpanded(btn, false);
        });
      });
    }

    var emptyOnlyCheck = rootEl.querySelector('[data-tree-action="empty-only"]');
    if (emptyOnlyCheck) {
      emptyOnlyCheck.addEventListener("change", function () {
        var enabled = emptyOnlyCheck.checked;
        if (enabled) {
          // Temporarily fully expand so a collapsed ancestor can't hide an
          // empty descendant it's the only path to — this is a view
          // filter, not a real preference (see the pagehide handler above,
          // which skips saving while this is checked).
          rootEl.querySelectorAll(".tree-toggle").forEach(function (btn) {
            setExpanded(btn, true);
          });
        } else {
          var restore = loadExpandedState(rootEl);
          rootEl.querySelectorAll(".tree-toggle").forEach(function (btn) {
            var li = btn.closest("li.tree-node-wrap");
            var id = li && li.dataset.id;
            var expanded = id && Object.prototype.hasOwnProperty.call(restore, id)
              ? restore[id]
              : isTopLevel(li);
            setExpanded(btn, expanded);
          });
        }
        rootEl.querySelectorAll("li.tree-node-wrap").forEach(function (li) {
          var isEmpty = li.dataset.empty === "1";
          li.classList.toggle("d-none", enabled && !isEmpty);
        });
      });
    }
  }

  // ---------------------------------------------------------------------
  // Drag to reorder / reparent
  // ---------------------------------------------------------------------
  function initSortable(rootEl) {
    var reorderUrl = rootEl.dataset.reorderUrl;
    var editable = rootEl.dataset.editable === "1";
    if (!editable || !reorderUrl || !window.Sortable) return;

    rootEl.querySelectorAll("ul.tree-children, ul.tree-root").forEach(function (ul) {
      new Sortable(ul, {
        group: "tree",
        animation: 150,
        fallbackOnBody: true,
        swapThreshold: 0.65,
        onEnd: sync,
      });
    });

    function sync() {
      var moves = [];
      function walk(ul, parentId) {
        Array.prototype.forEach.call(ul.children, function (li, index) {
          var id = parseInt(li.dataset.id, 10);
          moves.push({ id: id, parent_id: parentId, sort_order: index });
          var childUl = li.querySelector(":scope > ul.tree-children");
          if (childUl) walk(childUl, id);
        });
      }
      walk(rootEl.querySelector("ul.tree-root"), null);

      fetch(reorderUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": window.CSRF_TOKEN || "",
        },
        body: JSON.stringify({ moves: moves }),
      }).then(function (r) {
        if (!r.ok) location.reload();
      });
    }
  }

  // ---------------------------------------------------------------------
  // Right-click context menu
  // ---------------------------------------------------------------------
  // Not gated on data-editable: a read-only viewer still gets the menu for
  // whichever non-destructive items the template chose to render (Categories
  // shows "View parts" to everyone). The template decides what exists; this
  // just wires up whatever it finds.
  function initContextMenu(rootEl) {
    var menu = document.querySelector(rootEl.dataset.contextMenu || "#nodeContextMenu");
    if (!menu) return;

    var titleEl = menu.querySelector("[data-ctx-title]");
    var contextLi = null;

    function closeMenu() {
      menu.classList.remove("show");
      contextLi = null;
    }

    rootEl.addEventListener("contextmenu", function (e) {
      var li = e.target.closest("li.tree-node-wrap");
      if (!li || !rootEl.contains(li)) return;
      e.preventDefault();
      contextLi = li;
      if (titleEl) titleEl.textContent = li.dataset.name || "";

      // Link items point at the node that was actually right-clicked.
      menu.querySelectorAll("[data-ctx-href]").forEach(function (a) {
        a.setAttribute("href", a.dataset.ctxHref.replace("{id}", li.dataset.id));
      });

      menu.style.left = e.clientX + "px";
      menu.style.top = e.clientY + "px";
      menu.classList.add("show");

      requestAnimationFrame(function () {
        var rect = menu.getBoundingClientRect();
        if (rect.right > window.innerWidth) menu.style.left = Math.max(0, window.innerWidth - rect.width - 8) + "px";
        if (rect.bottom > window.innerHeight) menu.style.top = Math.max(0, window.innerHeight - rect.height - 8) + "px";
      });
    });

    document.addEventListener("click", function (e) {
      if (!menu.contains(e.target)) closeMenu();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeMenu();
    });
    window.addEventListener("scroll", closeMenu, true);

    var ACTIONS = {
      add: function (li) {
        var idField = document.getElementById("addParentId");
        var nameField = document.getElementById("addParentName");
        if (idField) idField.value = li.dataset.id;
        if (nameField) nameField.textContent = li.dataset.name || "";
        showModal("addModal");
      },
      edit: function (li) {
        var form = document.getElementById("editForm");
        if (form) form.action = form.dataset.actionTemplate.replace("{id}", li.dataset.id);
        setField("editName", li.dataset.name);
        setField("editCode", li.dataset.code);
        showModal("editModal");
      },
      delete: function (li) {
        var form = document.getElementById("deleteForm");
        if (!form) return;
        if (!confirm(form.dataset.confirm.replace("{name}", li.dataset.name || ""))) return;
        form.action = form.dataset.actionTemplate.replace("{id}", li.dataset.id);
        form.submit();
      },
    };

    function setField(id, value) {
      var el = document.getElementById(id);
      if (el) el.value = value || "";
    }

    function showModal(id) {
      var el = document.getElementById(id);
      // Lazily resolved: this file can load before bootstrap.bundle.js, so
      // constructing a Modal at parse time would throw.
      if (el) bootstrap.Modal.getOrCreateInstance(el).show();
    }

    menu.querySelectorAll("[data-ctx-action]").forEach(function (item) {
      item.addEventListener("click", function () {
        if (!contextLi) return;
        var li = contextLi;
        var run = ACTIONS[item.dataset.ctxAction];
        closeMenu();
        if (run) run(li);
      });
    });
  }
})();
