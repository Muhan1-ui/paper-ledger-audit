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

  let nextId = 0;
  function evaluate(expression) {
    return new Promise((resolve) => {
      const id = ++nextId;
      socket.onmessage = (event) => {
        const message = JSON.parse(event.data);
        if (message.id === id) resolve(message);
      };
      socket.send(JSON.stringify({
        id,
        method: "Runtime.evaluate",
        params: { expression, awaitPromise: true, returnByValue: true },
      }));
    });
  }

  const state = await evaluate(
    "JSON.stringify({status:document.getElementById('page-status').textContent,generated:document.getElementById('generated-at').textContent,settled:document.getElementById('settled-records').textContent})",
  );
  const request = await evaluate(
    "fetch('/api/summary',{headers:{Accept:'application/json'}}).then(async function(response){return {ok:response.ok,status:response.status,body:await response.text()};}).catch(function(error){return {error:String(error),name:error.name};})",
  );
  await evaluate(
    "window.__dashboardCaught=[];window.__dashboardOriginalCatch=Promise.prototype.catch;Promise.prototype.catch=function(handler){return window.__dashboardOriginalCatch.call(this,function(error){window.__dashboardCaught.push(error&&error.stack||String(error));return handler(error);});};document.getElementById('reload').click();new Promise(function(resolve){setTimeout(resolve,500);})",
  );
  const caught = await evaluate("JSON.stringify(window.__dashboardCaught)");
  const afterReload = await evaluate(
    "JSON.stringify({status:document.getElementById('page-status').textContent,generated:document.getElementById('generated-at').textContent,settled:document.getElementById('settled-records').textContent,disabled:document.getElementById('reload').disabled})",
  );
  console.log(JSON.stringify({ state, request, caught, afterReload }, null, 2));
  socket.close();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
