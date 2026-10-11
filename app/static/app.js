/* Weekend Micro-Adventure front end.
 *
 * No framework, no build step. Two rules worth keeping:
 *   1. Model output is inserted with textContent, never innerHTML. The card text comes
 *      from an LLM, so it is treated as untrusted.
 *   2. "Not feeling it" works once. After that the button is disabled on purpose.
 *      One decision per screen is the whole point of this app.
 */

(function () {
  "use strict";

  var TIMEOUT_MS = 20000;
  var API = "/api/adventure";

  var state = {
    time_budget: "half_day",
    transport: "foot",
    vibe: "quiet",
    exclude_ids: [],
    rerollUsed: false,
    lastCandidateId: null,
    loadedAt: performance.now()
  };

  var els = {
    send: document.getElementById("send"),
    reroll: null,
    location: document.getElementById("location"),
    useLocation: document.getElementById("use-location"),
    locationHint: document.getElementById("location-hint"),
    result: document.getElementById("result"),
    timer: document.getElementById("timer"),
    template: document.getElementById("tpl-card")
  };

  /* ------------------------------------------------------------- helpers */

  function setTimer() {
    var seconds = (performance.now() - state.loadedAt) / 1000;
    els.timer.textContent = "Time on screen: " + seconds.toFixed(1) + "s";
  }

  function showStatus(text, isError) {
    els.result.textContent = "";
    var p = document.createElement("p");
    p.className = isError ? "status error" : "status";
    p.textContent = text;
    els.result.appendChild(p);
  }

  function showHint(text, isError) {
    els.locationHint.hidden = !text;
    els.locationHint.textContent = text || "";
    els.locationHint.className = isError ? "hint error" : "hint";
  }

  function setBusy(busy) {
    els.send.disabled = busy;
    els.send.textContent = busy ? "Finding a place..." : "Send me out";
    els.result.setAttribute("aria-busy", busy ? "true" : "false");
    if (els.reroll) els.reroll.disabled = busy || state.rerollUsed;
  }

  /* ------------------------------------------------------------ rendering */

  function fill(node, selector, value) {
    var field = node.querySelector('[data-field="' + selector + '"]');
    if (field) field.textContent = value == null ? "" : String(value);
    return field;
  }

  function formatDuration(minutes) {
    if (minutes == null) return "";
    var m = Number(minutes);
    if (m >= 60) {
      var h = Math.floor(m / 60);
      var rest = m % 60;
      return rest ? h + " h " + rest + " min" : h + (h === 1 ? " hour" : " hours");
    }
    return m + " min";
  }

  function renderCard(card) {
    var node = els.template.content.cloneNode(true);
    var root = node.querySelector(".card");

    fill(root, "weather", card.weather_summary);
    fill(root, "title", card.title);
    fill(root, "place", card.place_name + (card.address ? " · " + card.address : ""));
    fill(root, "duration", formatDuration(card.duration_minutes));
    fill(root, "leave_by", card.leave_by);
    fill(root, "getting_there", card.getting_there);
    fill(root, "tip", card.tip);
    fill(root, "check", card.check_before_you_go);

    var list = root.querySelector('[data-field="plan"]');
    (card.plan || []).forEach(function (step) {
      var li = document.createElement("li");
      li.textContent = step;
      list.appendChild(li);
    });

    var link = root.querySelector('[data-field="maps"]');
    // Only ever link to Google Maps. A stray value should not become a javascript: URL.
    if (card.maps_link && String(card.maps_link).indexOf("https://www.google.com/maps/") === 0) {
      link.setAttribute("href", card.maps_link);
    } else {
      link.remove();
    }

    var reroll = root.querySelector("#reroll");
    if (state.rerollUsed) {
      reroll.disabled = true;
      reroll.textContent = "That's the one adventure";
    }

    els.result.textContent = "";
    els.result.appendChild(node);
    els.reroll = reroll;

    reroll.addEventListener("click", onReroll);
    return card;
  }

  /* -------------------------------------------------------------- requests */

  function buildBody() {
    var text = (els.location.value || "").trim();
    var body = {
      time_budget: state.time_budget,
      transport: state.transport,
      vibe: state.vibe,
      exclude_ids: state.exclude_ids.slice()
    };
    if (text) {
      body.location_text = text;
    } else if (state.gps) {
      body.lat = state.gps.lat;
      body.lng = state.gps.lng;
    }
    return body;
  }

  function onResponse(data) {
    if (data && data.empty) {
      showStatus(data.message || "Nothing good nearby. Try again.", false);
      return;
    }
    if (!data || !data.place_name) {
      showStatus("Something went wrong. Try again.", true);
      return;
    }
    state.lastCandidateId = data.candidate_id;
    renderCard(data);
  }

  function request() {
    var body = buildBody();
    if (!body.location_text && !body.lat) {
      showHint("Enter a city, or use your location.", true);
      showStatus("Something went wrong. Try again.", true);
      return;
    }

    showHint("", false);
    setBusy(true);
    showStatus("Finding a place...", false);

    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, TIMEOUT_MS);

    fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal
    })
      .then(function (res) {
        if (!res.ok && res.status !== 200) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(onResponse)
      .catch(function () {
        showStatus("Something went wrong. Try again.", true);
      })
      .then(function () {
        clearTimeout(timer);
        setBusy(false);
      });
  }

  function onSend() {
    setTimer();
    request();
  }

  function onReroll() {
    if (state.rerollUsed) return;
    if (state.lastCandidateId) state.exclude_ids.push(state.lastCandidateId);
    state.rerollUsed = true;
    request();
  }

  /* -------------------------------------------------------------- controls */

  function initSegments() {
    var buttons = document.querySelectorAll("[data-group]");
    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener("click", function () {
        var group = button.getAttribute("data-group");
        state[group] = button.getAttribute("data-value");

        var siblings = document.querySelectorAll('[data-group="' + group + '"]');
        Array.prototype.forEach.call(siblings, function (other) {
          other.setAttribute("aria-pressed", other === button ? "true" : "false");
        });
      });
    });
  }

  function initLocation() {
    els.useLocation.addEventListener("click", function () {
      if (!navigator.geolocation) {
        showHint("This browser cannot share a location.", true);
        return;
      }
      showHint("Asking your browser...");
      navigator.geolocation.getCurrentPosition(
        function (position) {
          state.gps = { lat: position.coords.latitude, lng: position.coords.longitude };
          els.location.value = "";
          showHint("Using your location.", false);
        },
        function () {
          showHint("Could not get your location. Type a city instead.", true);
        },
        { enableHighAccuracy: false, timeout: 10000 }
      );
    });
  }

  initSegments();
  initLocation();
  els.send.addEventListener("click", onSend);
})();