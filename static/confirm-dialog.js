/*
 * Shared application confirmation dialog.
 *
 * Exposes window.karaokeConfirm(options) -> Promise<boolean>. The dialog is
 * created lazily and reused, so pages pay no markup cost until they need it.
 * Native <dialog> supplies the modal behaviour; this module adds the shared
 * styling hooks, focus restoration, and Escape/backdrop handling.
 */
(() => {
  "use strict";

  const FOCUSABLE = [
    "button:not([disabled])",
    "[href]",
    "input:not([disabled])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    "[tabindex]:not([tabindex='-1'])"
  ].join(",");

  let dialog = null;
  let titleEl = null;
  let messageEl = null;
  let cancelButton = null;
  let confirmButton = null;
  let resolveCurrent = null;
  let previousFocus = null;

  function focusAfterClose(element) {
    if (!element || !element.isConnected) return;
    element.focus({ preventScroll: true });
  }

  function settle(confirmed) {
    if (!dialog || !dialog.open) return;
    const resolve = resolveCurrent;
    const focusTarget = previousFocus;
    resolveCurrent = null;
    previousFocus = null;
    confirmButton.disabled = true;
    cancelButton.disabled = true;
    dialog.close();
    document.body.classList.remove("confirm-dialog-open");
    if (resolve) resolve(confirmed);
    focusAfterClose(focusTarget);
  }

  function build() {
    if (dialog) return;

    dialog = document.createElement("dialog");
    dialog.className = "confirm-dialog";
    dialog.tabIndex = -1;
    dialog.setAttribute("aria-labelledby", "confirm-dialog-title");
    dialog.setAttribute("aria-describedby", "confirm-dialog-message");
    dialog.innerHTML = `
      <div class="confirm-dialog-content">
        <h2 class="confirm-dialog-title" id="confirm-dialog-title"></h2>
        <p class="confirm-dialog-message" id="confirm-dialog-message"></p>
        <div class="confirm-dialog-actions">
          <button type="button" class="confirm-dialog-button confirm-dialog-cancel"></button>
          <button type="button" class="confirm-dialog-button confirm-dialog-confirm"></button>
        </div>
      </div>`;

    titleEl = dialog.querySelector(".confirm-dialog-title");
    messageEl = dialog.querySelector(".confirm-dialog-message");
    cancelButton = dialog.querySelector(".confirm-dialog-cancel");
    confirmButton = dialog.querySelector(".confirm-dialog-confirm");

    cancelButton.addEventListener("click", () => settle(false));
    confirmButton.addEventListener("click", () => settle(true));

    // Escape and backdrop clicks cancel. `cancel` fires for Escape; handling it
    // ourselves keeps every close path on the same focus-restoration route.
    dialog.addEventListener("cancel", event => {
      event.preventDefault();
      settle(false);
    });

    dialog.addEventListener("click", event => {
      if (event.target === dialog) settle(false);
    });

    // Native modal dialogs keep the rest of the page inert, but an explicit
    // Tab wrap also covers browsers whose focus containment is less strict.
    dialog.addEventListener("keydown", event => {
      if (event.key !== "Tab") return;
      const focusables = Array.from(dialog.querySelectorAll(FOCUSABLE));
      if (!focusables.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });

    dialog.addEventListener("close", () => {
      document.body.classList.remove("confirm-dialog-open");
      if (!resolveCurrent) return;
      // The browser closed the dialog without going through settle().
      const resolve = resolveCurrent;
      const focusTarget = previousFocus;
      resolveCurrent = null;
      previousFocus = null;
      resolve(false);
      focusAfterClose(focusTarget);
    });

    document.body.appendChild(dialog);
  }

  function confirmAction(options = {}) {
    build();
    if (dialog.open) return Promise.resolve(false);

    const tone = options.tone === "danger" ? "danger" : "default";
    const title = String(options.title || "Are you sure?");
    const message = String(options.message || "");
    const cancelLabel = String(options.cancelLabel || "Cancel");
    const confirmLabel = String(options.confirmLabel || "Confirm");

    titleEl.textContent = title;
    messageEl.textContent = message;
    messageEl.hidden = !message;
    cancelButton.textContent = cancelLabel;
    confirmButton.textContent = confirmLabel;
    cancelButton.disabled = false;
    confirmButton.disabled = false;
    dialog.classList.toggle("is-destructive", tone === "danger");

    previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    document.body.classList.add("confirm-dialog-open");

    // Destructive prompts start on Cancel so Enter/Space does not perform the
    // destructive action by accident. Non-destructive prompts can opt into
    // focusing the confirmation button.
    const focusConfirm = options.focus === "confirm" ||
      (options.focus !== "cancel" && tone !== "danger");
    const preferred = focusConfirm ? confirmButton : cancelButton;

    return new Promise(resolve => {
      resolveCurrent = resolve;
      try {
        dialog.showModal();
      } catch {
        resolveCurrent = null;
        previousFocus = null;
        document.body.classList.remove("confirm-dialog-open");
        resolve(false);
        return;
      }
      preferred.focus({ preventScroll: true });
    });
  }

  window.karaokeConfirm = confirmAction;
})();
