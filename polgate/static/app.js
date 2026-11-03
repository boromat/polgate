/*
 * Behaviour for the hub.
 *
 * Everything lives here rather than in the templates so the Content-Security-Policy can stay
 * strict: no inline <script>, no inline event handler, nothing built from a string. Elements
 * declare what they want with data- attributes and this file listens for it, which also means a
 * partial swapped in by HTMX behaves the same as markup rendered by the server.
 */
(function () {
  "use strict";

  /* Server error codes that deserve their own explanation, mapped to the data- attribute holding
     the translated text. Anything else falls back to the generic error label. */
  var ERROR_LABELS = {
    refused_unmasked: "refusedLabel",
    judge_unavailable: "judgeLabel",
  };

  var TOKEN = /<[A-Z_]+_\d+>/g;

  function messageBox() {
    return document.getElementById("messages");
  }

  function scrollMessages() {
    var box = messageBox();
    if (box) {
      box.scrollTop = box.scrollHeight;
    }
  }

  /* Grow a textarea with what is typed into it. scrollHeight leaves the borders out, and a
     border-box field set to exactly that height scrolls by the width of its borders. */
  function autogrow(field) {
    var limit = parseInt(field.dataset.autogrow, 10) || 200;
    field.style.height = "auto";
    var borders = field.offsetHeight - field.clientHeight;
    field.style.height = Math.min(field.scrollHeight + borders, limit) + "px";
  }

  /* The server stores and renders UTC. Shown as is, a message sent at 17:23 in Warsaw read 15:23,
     so every timestamp is rewritten in the reader's own time zone. */
  function localTime(element) {
    var moment = new Date(element.getAttribute("datetime"));
    if (isNaN(moment.getTime())) {
      return;
    }
    element.textContent = moment.toLocaleTimeString(document.documentElement.lang || [], {
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function setWaiting(sink, label) {
    var note = document.createElement("span");
    note.className = "waiting";
    note.textContent = label;
    sink.replaceChildren(note);
  }

  function showBadge(container, kind, label) {
    var badge = document.createElement("span");
    badge.className = "badge " + kind;
    badge.textContent = label;
    container.replaceChildren(badge);
    container.hidden = false;
  }

  /* Once the message is masked and stored, show under it what the provider is about to receive:
     how many values became tokens, or plainly that nothing did, and the same preview button a
     reloaded conversation has. */
  function showSent(question, maskedText, messageId) {
    var meta = question && question.querySelector("[data-sent-meta]");
    if (!meta) {
      return;
    }
    var count = (maskedText.match(TOKEN) || []).length;
    var time = meta.querySelector("[data-local-time]");
    time.setAttribute("datetime", new Date().toISOString());
    localTime(time);
    meta.querySelector("[data-masked-count]").hidden = count === 0;
    meta.querySelector("[data-count]").textContent = String(count);
    meta.querySelector("[data-nothing-masked]").hidden = count !== 0;

    var button = meta.querySelector("[data-preview-button]");
    var target = question.querySelector("[data-preview-target]");
    if (messageId && button && target && window.htmx) {
      target.id = "preview-" + messageId;
      button.setAttribute("hx-get", button.dataset.previewBase + messageId + "/preview");
      button.setAttribute("hx-target", "#" + target.id);
      button.setAttribute("aria-controls", target.id);
      button.hidden = false;
      window.htmx.process(button);
    }
    meta.hidden = false;
  }

  /* Read one answer from the server's event stream into the assistant bubble. */
  function connectStream(article) {
    if (article.dataset.wired === "1") {
      return;
    }
    article.dataset.wired = "1";
    var sink = article.querySelector("[data-sink]");
    var status = article.querySelector("[data-status]");
    var question = article.previousElementSibling;
    var started = false;
    var finished = false;
    var source = new EventSource(article.dataset.stream);
    article.setAttribute("aria-busy", "true");

    function read(event) {
      try {
        return JSON.parse(event.data || "{}");
      } catch (error) {
        return {};
      }
    }

    function finish() {
      finished = true;
      source.close();
      article.removeAttribute("aria-busy");
      if (!started) {
        sink.replaceChildren();
      }
    }

    source.addEventListener("preview", function (event) {
      var data = read(event);
      showSent(question, data.text || "", data.detail);
      if (!started) {
        setWaiting(sink, article.dataset.waitingLabel);
      }
      scrollMessages();
    });

    source.addEventListener("token", function (event) {
      if (!started) {
        sink.textContent = "";
        started = true;
      }
      sink.textContent += read(event).text || "";
      scrollMessages();
    });

    source.addEventListener("done", function (event) {
      if (read(event).detail) {
        showBadge(status, "warn", article.dataset.warnLabel);
      }
      finish();
    });

    /* "error" arrives from two places: the server's own error event, which carries data, and the
       browser reporting a dropped connection, which does not. Either way the turn is over and the
       source must be closed, or EventSource reconnects to a turn already taken and gets a 409. */
    source.addEventListener("error", function (event) {
      if (finished) {
        return;
      }
      var labelKey = ERROR_LABELS[read(event).detail];
      showBadge(status, "error", (labelKey && article.dataset[labelKey]) || article.dataset.errorLabel);
      finish();
    });
  }

  /* Apply every enhancement inside `root`, and to `root` itself: HTMX reports each swapped-in
     element as its own load event, so the node that needs wiring is often the root. */
  function enhance(root) {
    if (!root || !root.querySelectorAll) {
      return;
    }
    each(root, "[data-stream]", connectStream);
    each(root, "textarea[data-autogrow]", autogrow);
    each(root, "time[data-local-time][datetime]", localTime);
    each(root, "[data-width]", function (bar) {
      bar.style.width = bar.dataset.width + "%";
    });
  }

  function each(root, selector, apply) {
    if (root.matches && root.matches(selector)) {
      apply(root);
    }
    root.querySelectorAll(selector).forEach(apply);
  }

  document.addEventListener("input", function (event) {
    if (event.target.matches("textarea[data-autogrow]")) {
      autogrow(event.target);
    }
  });

  /* Enter sends and Shift+Enter starts a new line, as in every other chat window. The Enter that
     confirms an IME composition is not a send, and a message of nothing but spaces is not sent. */
  document.addEventListener("keydown", function (event) {
    var field = event.target;
    if (!field.matches || !field.matches("[data-submit-on-enter]")) {
      return;
    }
    if (event.key !== "Enter" || event.shiftKey || event.isComposing || !field.form) {
      return;
    }
    event.preventDefault();
    if (field.value.trim()) {
      field.form.requestSubmit();
    }
  });

  document.addEventListener("change", function (event) {
    var field = event.target;
    if (field.matches("[data-autosubmit]") && field.form) {
      field.form.submit();
      return;
    }
    if (field.matches("input[type=file][data-names]")) {
      var label = field.form.querySelector("[data-name]");
      if (label) {
        label.textContent = field.files[0] ? field.files[0].name : "";
      }
    }
  });

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-print]");
    if (trigger) {
      event.preventDefault();
      window.print();
    }
  });

  document.body.addEventListener("htmx:afterRequest", function (event) {
    var source = event.detail.elt;
    if (!event.detail.successful || !source) {
      return;
    }
    if (source.matches("[data-remove-on-success]")) {
      source.remove();
    }
    if (source.matches("form[data-reset-on-success]")) {
      source.reset();
      source.querySelectorAll("textarea[data-autogrow]").forEach(autogrow);
      var label = source.querySelector("[data-name]");
      if (label) {
        label.textContent = "";
      }
    }
    scrollMessages();
  });

  document.body.addEventListener("htmx:load", function (event) {
    enhance(event.detail.elt);
  });

  enhance(document);
  scrollMessages();
})();
