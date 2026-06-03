import { afterEach, describe, expect, it, vi } from "vitest";

import { currentLocale, FLAGS, initLocale, LOCALES, loadLocale, pickLocale, RTL, t, tPlural } from "./index";

describe("locale set", () => {
  it("is the snapper 45-locale palette with ar/fa/he marked RTL", () => {
    expect(LOCALES).toHaveLength(45);
    expect(LOCALES).toContain("en");
    expect(LOCALES).toContain("pl");
    expect(RTL.has("ar")).toBe(true);
    expect(RTL.has("fa")).toBe(true);
    expect(RTL.has("he")).toBe(true);
    expect(RTL.has("en")).toBe(false);
  });

  it("has a non-empty flag for every locale", () => {
    expect(Object.keys(FLAGS)).toHaveLength(LOCALES.length);
    for (const code of LOCALES) {
      expect(FLAGS[code].length).toBeGreaterThan(0);
    }
  });
});

describe("pickLocale", () => {
  it("matches an exact supported code", () => {
    expect(pickLocale(["pl"])).toBe("pl");
    expect(pickLocale(["de"])).toBe("de");
  });
  it("falls back to the primary subtag (en-US → en, zh-CN → zh, pt-BR → pt)", () => {
    expect(pickLocale(["en-US"])).toBe("en");
    expect(pickLocale(["zh-CN"])).toBe("zh");
    expect(pickLocale(["pt-BR"])).toBe("pt");
  });
  it("maps Chinese script/region tags (zh-CN→zh; zh-TW/zh-HK/zh-Hant→zh-Hant)", () => {
    expect(pickLocale(["zh-CN"])).toBe("zh");
    expect(pickLocale(["zh-SG"])).toBe("zh");
    expect(pickLocale(["zh-TW"])).toBe("zh-Hant");
    expect(pickLocale(["zh-HK"])).toBe("zh-Hant");
    expect(pickLocale(["zh-Hant"])).toBe("zh-Hant");
    expect(pickLocale(["zh-Hant-TW"])).toBe("zh-Hant");
  });

  it("skips unsupported preferences to the next match", () => {
    expect(pickLocale(["xx", "fr-FR"])).toBe("fr");
  });
  it("defaults to en when nothing matches or the list is empty", () => {
    expect(pickLocale(["xx", "zz"])).toBe("en");
    expect(pickLocale([])).toBe("en");
  });
});

describe("t / tPlural", () => {
  afterEach(async () => {
    await loadLocale("en"); // reset shared module state for the next test
  });

  it("returns English by default and interpolates params", () => {
    expect(t("login.submit")).toBe("Sign in");
    expect(t("user.loggedInAs", { user: "tata" })).toBe("signed in: tata");
  });

  it("translates after loading a locale, falling back to English for missing keys", async () => {
    await loadLocale("pl");
    expect(currentLocale()).toBe("pl");
    expect(t("login.submit")).toBe("Zaloguj");
    expect(t("header.title")).toBe("hestia"); // pl omits header.title → English fallback
  });

  it("applies CLDR plural categories (en one/other; pl one/few/many)", async () => {
    expect(tPlural("rooms.deviceCount", 1)).toBe("1 device");
    expect(tPlural("rooms.deviceCount", 5)).toBe("5 devices");
    await loadLocale("pl");
    expect(tPlural("rooms.deviceCount", 1)).toBe("1 urządzenie"); // one
    expect(tPlural("rooms.deviceCount", 2)).toBe("2 urządzenia"); // few
    expect(tPlural("rooms.deviceCount", 5)).toBe("5 urządzeń"); // many
  });
});

describe("loadLocale / initLocale", () => {
  afterEach(async () => {
    vi.unstubAllGlobals();
    await loadLocale("en");
  });

  function stubStoredLocale(value: string): void {
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => (k === "hestia.locale" ? value : null),
      setItem: () => undefined,
      removeItem: () => undefined,
      clear: () => undefined,
      key: () => null,
      length: 1,
    });
  }

  it("sets <html lang> + dir=ltr for a shipped LTR locale", async () => {
    await loadLocale("pl");
    expect(document.documentElement.lang).toBe("pl");
    expect(document.documentElement.dir).toBe("ltr");
  });

  it("falls back to English when a catalog is not shipped yet", async () => {
    await loadLocale("de"); // no de catalog in PR-A
    expect(currentLocale()).toBe("en");
    expect(document.documentElement.dir).toBe("ltr");
  });

  it("initLocale picks the best browser locale, loads + applies it", async () => {
    const code = await initLocale(["pl-PL", "en"]);
    expect(code).toBe("pl");
    expect(currentLocale()).toBe("pl");
    expect(document.documentElement.lang).toBe("pl");
  });

  it("initLocale honors a stored override over the browser preferences", async () => {
    stubStoredLocale("pl");
    const code = await initLocale(["de", "en"]); // browser says de, override says pl
    expect(code).toBe("pl");
  });

  it("initLocale ignores an invalid stored override and falls back to the browser", async () => {
    stubStoredLocale("zz");
    const code = await initLocale(["fr", "en"]);
    expect(code).toBe("fr");
  });
});
