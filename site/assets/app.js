// Client behaviour: catalog filtering (tag + text, mirrored into ?tag=&q= so a filtered view is
// a link) and the copy buttons. The pages are complete without it.

function setupCopy() {
  for (const block of document.querySelectorAll("[data-copy]")) {
    const button = block.querySelector(".copy-button");
    let resetTimer;
    button.addEventListener("click", async () => {
      // A new click stops the running timer at once, so it cannot reset this click's
      // confirmation while the clipboard write is still pending...
      clearTimeout(resetTimer);
      const text = block.dataset.copy;
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "Copied";
      } catch {
        // No clipboard permission (plain http, old browser): select the text for a manual copy.
        const range = document.createRange();
        range.selectNodeContents(block.querySelector("code"));
        getSelection().removeAllRanges();
        getSelection().addRange(range);
        button.textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? "Press ⌘C" : "Press Ctrl+C";
      }
      // ...and again before arming, as an earlier click whose write was pending may have armed
      // one meanwhile: the last write to land owns the only timer.
      clearTimeout(resetTimer);
      resetTimer = setTimeout(() => (button.textContent = "Copy"), 1600);
    });
  }
}

function setupCatalog() {
  const cards = [...document.querySelectorAll("#cards .card")];
  if (!cards.length) return;
  const search = document.getElementById("search");
  const buttons = [...document.querySelectorAll("button[data-tag]")];
  const status = document.getElementById("status");
  const empty = document.getElementById("empty");
  const params = new URLSearchParams(location.search);
  let tag = params.get("tag") ?? "";
  search.value = params.get("q") ?? "";

  function apply() {
    const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    for (const card of cards) {
      const visible =
        (!tag || card.dataset.tags.split(" ").includes(tag)) &&
        words.every((word) => card.dataset.search.includes(word));
      card.hidden = !visible;
      if (visible) shown += 1;
    }
    for (const button of buttons) button.setAttribute("aria-pressed", String(button.dataset.tag === tag));
    status.textContent = shown === cards.length ? `${shown} dictionaries` : `${shown} of ${cards.length} dictionaries`;
    empty.hidden = shown !== 0;
    const next = new URLSearchParams();
    if (tag) next.set("tag", tag);
    if (search.value.trim()) next.set("q", search.value.trim());
    const query = next.toString();
    try {
      history.replaceState(null, "", query ? `?${query}` : location.pathname);
    } catch {
      // Safari refuses more than 100 history updates in 30 seconds; the filter still applies.
    }
  }

  for (const button of buttons) {
    button.addEventListener("click", () => {
      tag = tag === button.dataset.tag ? "" : button.dataset.tag;
      apply();
    });
  }
  search.addEventListener("input", apply);
  document.getElementById("clear").addEventListener("click", () => {
    tag = "";
    search.value = "";
    apply();
    search.focus();
  });
  apply();
}

setupCopy();
setupCatalog();
