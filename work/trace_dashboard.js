(async () => {
  const targets = await (await fetch("http://127.0.0.1:9224/json/list")).json();
  const target = targets.find(
    (item) => item.type === "page" && item.url === "http://127.0.0.1:8767/",
  );
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  const events = [];
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  });
  let nextId = 0;
  const pending = new Map();
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      pending.get(message.id)(message.result);
      pending.delete(message.id);
    } else if (message.method === "Runtime.exceptionThrown" || message.method === "Log.entryAdded") {
      events.push(message);
    }
  };
  function call(method, params) {
    return new Promise((resolve) => {
      const id = ++nextId;
      pending.set(id, resolve);
      socket.send(JSON.stringify({ id, method, params }));
    });
  }
  await call("Runtime.enable");
  await call("Log.enable");
  await call("Runtime.evaluate", {
    expression: "document.getElementById('reload').click()",
    returnByValue: true,
  });
  await new Promise((resolve) => setTimeout(resolve, 4000));
  const state = await call("Runtime.evaluate", {
    expression: "JSON.stringify({status:document.getElementById('page-status').textContent,disabled:document.getElementById('reload').disabled})",
    returnByValue: true,
  });
  console.log(JSON.stringify({ events, state: state.result.value }, null, 2));
  socket.close();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
