const fs = require("fs");

(async () => {
  const targets = await (await fetch("http://127.0.0.1:9224/json/list")).json();
  const target = targets.find(
    (item) => item.type === "page" && item.url === "http://127.0.0.1:8767/",
  );
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  });

  let nextId = 0;
  const pending = new Map();
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    const resolve = pending.get(message.id);
    if (resolve) {
      pending.delete(message.id);
      resolve(message.result);
    }
  };
  function call(method, params) {
    return new Promise((resolve) => {
      const id = ++nextId;
      pending.set(id, resolve);
      socket.send(JSON.stringify({ id, method, params }));
    });
  }
  await call("Page.reload", { ignoreCache: true });
  await new Promise((resolve) => setTimeout(resolve, 1000));
  await call("Runtime.evaluate", {
    expression: "document.getElementById('reload').click()",
    returnByValue: true,
  });
  await new Promise((resolve) => setTimeout(resolve, 4000));
  async function capture(width, output) {
    await call("Emulation.setDeviceMetricsOverride", {
      width,
      height: 1000,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await new Promise((resolve) => setTimeout(resolve, 200));
    const image = await call("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(output, Buffer.from(image.data, "base64"));
  }
  await capture(1440, "work/dashboard-live-desktop.png");
  await capture(600, "work/dashboard-live-mobile.png");
  const state = await call("Runtime.evaluate", {
    expression: "JSON.stringify({status:document.getElementById('page-status').textContent,settled:document.getElementById('settled-records').textContent,trend:document.getElementById('trend-chart').children.length,reliability:document.getElementById('reliability-chart').children.length})",
    returnByValue: true,
  });
  console.log(state.result.value);
  socket.close();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
