(() => {
  "use strict";

  const mobileMenu = document.getElementById("mobile-menu");
  const menuToggle = document.getElementById("menu-toggle");
  const toast = document.getElementById("toast");
  const prefersReducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  let toastTimer = 0;

  function showToast(message) {
    if (!toast) return;
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.setAttribute("aria-hidden", "false");
    toast.classList.add("is-visible");
    toastTimer = window.setTimeout(() => {
      toast.classList.remove("is-visible");
      toast.setAttribute("aria-hidden", "true");
    }, 4200);
  }

  function setMenu(open) {
    if (!mobileMenu || !menuToggle) return;
    mobileMenu.hidden = !open;
    document.body.classList.toggle("menu-open", open);
    menuToggle.setAttribute("aria-expanded", String(open));
    menuToggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
  }

  function updateActiveLink(target) {
    document.querySelectorAll(".desktop-nav a, .menu-sheet a").forEach((link) => {
      const isActive = link.getAttribute("href") === target;
      link.classList.toggle("is-active", isActive);
    });
  }

  function animateCounter(element, index) {
    const target = Number(element.dataset.target || 0);
    const decimals = Number(element.dataset.decimals || 0);
    const duration = 1500 + index * 80;
    const startOffset = 480 + index * 90;
    const format = (value) => value.toLocaleString("en-US", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });

    if (prefersReducedMotion) {
      element.textContent = format(target);
      return;
    }

    window.setTimeout(() => {
      const start = performance.now();
      const easeOutCubic = (value) => 1 - Math.pow(1 - value, 3);
      const tick = (now) => {
        const progress = Math.min(1, (now - start) / duration);
        element.textContent = format(target * easeOutCubic(progress));
        if (progress < 1) window.requestAnimationFrame(tick);
      };
      window.requestAnimationFrame(tick);
    }, startOffset);
  }

  function initCounters() {
    const counters = [...document.querySelectorAll("[data-counter]")];
    if (!counters.length) return;
    const start = (items) => items.forEach((item, index) => animateCounter(item, index));
    if (!("IntersectionObserver" in window) || prefersReducedMotion) {
      start(counters);
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        start([entry.target]);
        observer.unobserve(entry.target);
      });
    }, { threshold: .25 });
    counters.forEach((counter) => observer.observe(counter));
  }

  function bindNavigation() {
    menuToggle?.addEventListener("click", () => {
      setMenu(menuToggle.getAttribute("aria-expanded") !== "true");
    });

    document.querySelectorAll("[data-menu-close], .menu-sheet a").forEach((element) => {
      element.addEventListener("click", () => setMenu(false));
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") setMenu(false);
    });

    window.addEventListener("resize", () => {
      if (window.innerWidth > 720) setMenu(false);
    }, { passive: true });

    document.querySelectorAll(".desktop-nav a, .menu-sheet a").forEach((link) => {
      link.addEventListener("click", () => updateActiveLink(link.getAttribute("href") || "#home"));
    });

    document.querySelector("[data-cta]")?.addEventListener("click", () => {
      window.location.href = "lab.html";
    });

    document.querySelectorAll(".sign-in, .mobile-sign-in").forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        setMenu(false);
        showToast("Sign-in is reserved for the connected operator build.");
      });
    });
  }

  initCounters();
  bindNavigation();
})();
