const readline = require("node:readline");
const vm = require("node:vm");
const { performance } = require("node:perf_hooks");
const nodeCrypto = require("node:crypto");

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
const pendingReqs = new Map();
const messageListeners = [];
let started = false;

function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function fail(error) {
  emit({ type: "error", message: error instanceof Error ? error.message : String(error) });
  process.exit(1);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function installBrowserEnv(config) {
  const cookies = new Map();
  if (config.deviceId) cookies.set("oai-did", encodeURIComponent(config.deviceId));

  const defineGlobal = (name, value) => {
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  };
  defineGlobal("window", globalThis);
  defineGlobal("self", globalThis);
  defineGlobal("top", globalThis);
  defineGlobal("parent", globalThis);
  defineGlobal("performance", performance);
  defineGlobal("TextEncoder", TextEncoder);
  defineGlobal("crypto", {
    getRandomValues: (target) => nodeCrypto.webcrypto.getRandomValues(target),
    randomUUID: () => nodeCrypto.randomUUID(),
  });
  defineGlobal("btoa", (value) => Buffer.from(String(value), "binary").toString("base64"));
  defineGlobal("atob", (value) => Buffer.from(String(value), "base64").toString("binary"));
  defineGlobal("requestIdleCallback", (callback) => setTimeout(() => callback({ timeRemaining: () => 50, didTimeout: false }), 0));
  defineGlobal("cancelIdleCallback", (id) => clearTimeout(id));

  const pageUrl = new URL(config.pageUrl || "https://auth.openai.com/about-you");
  defineGlobal("location", pageUrl);
  defineGlobal("URL", URL);
  defineGlobal("URLSearchParams", URLSearchParams);
  defineGlobal("screen", {
    width: Number(config.screenWidth || 1920),
    height: Number(config.screenHeight || 1080),
  });
  defineGlobal("navigator", {
    userAgent: config.userAgent || "",
    language: "en-US",
    languages: ["en-US", "en"],
    hardwareConcurrency: Number(config.hardwareConcurrency || 16),
    cookieEnabled: true,
    vendor: "Google Inc.",
    product: "Gecko",
    webdriver: false,
  });

  const currentScript = { src: config.sdkUrl };
  const scripts = [currentScript];

  function makeIframe() {
    const listeners = new Map();
    const iframe = {
      style: {},
      src: "",
      contentWindow: {
        postMessage(message, targetOrigin) {
          const requestId = String(message && message.requestId ? message.requestId : "");
          if (!requestId) return;
          pendingReqs.set(requestId, { iframe, targetOrigin });
          emit({
            type: "sentinel_req",
            requestId,
            flow: String(message.flow || config.flow || ""),
            p: String(message.p || ""),
          });
        },
      },
      addEventListener(type, callback) {
        if (!listeners.has(type)) listeners.set(type, []);
        listeners.get(type).push(callback);
        if (type === "load" && iframe.loaded) setImmediate(callback);
      },
      loaded: false,
      _dispatch(type) {
        iframe.loaded = type === "load" || iframe.loaded;
        for (const callback of listeners.get(type) || []) setImmediate(callback);
      },
    };
    return iframe;
  }

  const head = {
    appendChild(element) {
      if (element && typeof element._dispatch === "function") element._dispatch("load");
      return element;
    },
  };

  defineGlobal("document", {
    currentScript,
    scripts,
    head,
    body: head,
    documentElement: {
      getAttribute(name) {
        return name === "data-build" ? (config.dataBuild || "") : null;
      },
    },
    createElement(tagName) {
      if (String(tagName).toLowerCase() === "iframe") return makeIframe();
      const element = { style: {}, src: "", addEventListener() {} };
      scripts.push(element);
      return element;
    },
    addEventListener() {},
    get cookie() {
      return [...cookies.entries()].map(([name, value]) => `${name}=${value}`).join("; ");
    },
    set cookie(value) {
      const [pair] = String(value || "").split(";");
      const index = pair.indexOf("=");
      if (index > 0) cookies.set(pair.slice(0, index).trim(), pair.slice(index + 1).trim());
    },
  });

  defineGlobal("addEventListener", (type, callback) => {
    if (type === "message") messageListeners.push(callback);
  });
  defineGlobal("removeEventListener", (type, callback) => {
    if (type !== "message") return;
    const index = messageListeners.indexOf(callback);
    if (index >= 0) messageListeners.splice(index, 1);
  });
}

async function run(config) {
  installBrowserEnv(config);
  vm.runInThisContext(String(config.sdkSource || ""), { filename: config.sdkUrl || "sentinel-sdk.js" });
  if (!globalThis.SentinelSDK || typeof globalThis.SentinelSDK.token !== "function") {
    throw new Error("SentinelSDK.token is unavailable");
  }

  const flow = String(config.flow || "");
  const token = await globalThis.SentinelSDK.token(flow);
  let soToken = "";
  if (config.includeSo && typeof globalThis.SentinelSDK.sessionObserverToken === "function") {
    await sleep(Number(config.observerWaitMs || 5000));
    soToken = (await globalThis.SentinelSDK.sessionObserverToken(flow)) || "";
  }
  emit({
    type: "result",
    token: token || "",
    soToken,
    sdkVersion: config.sdkVersion || "",
    sdkUrl: config.sdkUrl || "",
  });
  setTimeout(() => process.exit(0), 0);
}

rl.on("line", (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch (error) {
    fail(error);
    return;
  }

  if (message.type === "start") {
    if (started) return;
    started = true;
    run(message).catch(fail);
    return;
  }

  if (message.type === "sentinel_req_result") {
    const pending = pendingReqs.get(String(message.requestId || ""));
    if (!pending) return;
    pendingReqs.delete(String(message.requestId || ""));
    pending.iframe.contentWindow.__sentinelLastResult = message.result;
    const payload = message.error
      ? { type: "response", requestId: message.requestId, error: String(message.error) }
      : { type: "response", requestId: message.requestId, result: message.result };
    const origin = pending.targetOrigin || "https://sentinel.openai.com";
    const eventOrigin = (() => {
      try {
        return new URL(origin).origin;
      } catch {
        return origin;
      }
    })();
    setImmediate(() => {
      for (const listener of messageListeners) {
        listener({ source: pending.iframe.contentWindow, data: payload, origin: eventOrigin });
      }
    });
  }
});

rl.on("close", () => {
  if (!started) fail(new Error("missing start message"));
});