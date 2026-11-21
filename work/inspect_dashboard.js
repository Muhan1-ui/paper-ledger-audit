(async () => {
  const targets = await (await fetch("http://127.0.0.1:9223/json/list")).json();
  const target = targets.find(
    (item) => item.type === "page" && item.url === "http://127.0.0.1:8767/",
  );
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  });
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id === 1) {
      console.log(message.result.result.value);
      socket.close();
    }
  };
  socket.send(JSON.stringify({
    id: 1,
    method: "Runtime.evaluate",
    params: {
      expression: "JSON.stringify({status:document.getElementById('page-status').textContent,generated:document.getElementById('generated-at').textContent,settled:document.getElementById('settled-records').textContent,trend:document.getElementById('trend-chart').children.length,reliability:document.getElementById('reliability-chart').children.length,disabled:document.getElementById('reload').disabled})",
      returnByValue: true,
    },
  }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
