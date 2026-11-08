// Local, synthetic contract scenarios. No real credentials or account data.
const http = require('http');
let history = [];
const reply = (res, status, body) => {
  res.writeHead(status, {'Content-Type': 'application/json'});
  res.end(JSON.stringify(body));
};
http.createServer(async (req, res) => {
  // Postman's path router trims a trailing slash before forwarding to a member.
  const incomingPath = new URL(req.url, 'http://localhost').pathname;
  const path = incomingPath === '/account/login' ? '/account/login/' : incomingPath;
  if (path === '/__test/reset') { history = []; return reply(res, 200, {}); }
  if (path === '/__test/requests') return reply(res, 200, history);
  let body = req.body;
  if (body === undefined) {
    let raw = '';
    for await (const chunk of req) raw += chunk;
    try { body = JSON.parse(raw || '{}'); } catch { return reply(res, 400, {}); }
  }
  if (typeof body === 'string') body = JSON.parse(body);
  history.push({path, tenantId: body.app?.tenantId, bundle: body.app?.bundle,
                countryNo: body.countryNo, timeZone: body.app?.timeZone, language: body.language,
                hasAuthorization: !!req.headers.authorization,
                serialNumber: body.serialNumber, loginType: body.loginType,
                pitch: body.pitch, yaw: body.yaw});
  if (body.app?.tenantId !== 'provisionhome' || body.app?.bundle !== 'com.provisionhome.app') {
    return reply(res, 400, {result: -9999});
  }
  if (path === '/account/login/' && req.method === 'POST') {
    if (body.password === 'wrong') return reply(res, 200, {result: -9999});
    if (body.password === 'http-error') return reply(res, 503, {result: -9999});
    if (body.password === 'non-json') { res.writeHead(200); return res.end('<html>synthetic failure</html>'); }
    if (body.password === 'redirect') {
      res.writeHead(307, {Location: 'http://127.0.0.1:43862/never-forward-credentials'});
      return res.end();
    }
    if (body.password === 'missing-token') return reply(res, 200, {result: 0, data: {}});
    if (body.email !== 'owner@example.invalid' || body.password !== 'synthetic-password') {
      return reply(res, 200, {result: -9999});
    }
    return reply(res, 200, {result: 0, data: {token: {token: 'synthetic-account-token'}}});
  }
  if (path === '/device/rotate' && req.method === 'POST') {
    if (req.headers.authorization === 'expired-account-token') return reply(res, 200, {result: -1023});
    if (req.headers.authorization !== 'synthetic-account-token') return reply(res, 401, {});
    if (![body.pitch, body.yaw].every(value => Number.isFinite(value) && value >= -1 && value <= 1)) {
      return reply(res, 400, {result: -9999});
    }
    return reply(res, 200, {result: 0, data: {}});
  }
  if (path === '/device/getWebrtcTicket' && req.method === 'POST') {
    const token = req.headers.authorization;
    if (!token) return reply(res, 401, {});
    if (token === 'expired-account-token') return reply(res, 200, {result: -1023});
    const variants = ['synthetic-account-token', 'expired-ticket', 'untrusted-signal', 'bad-ticket'];
    if (!variants.includes(token)) return reply(res, 200, {result: -1025});
    const data = {id: 'synthetic-viewer', accessToken: 'synthetic-signaling-token',
                  groupId: body.serialNumber, traceId: String(Date.now()),
                  signalServer: 'wss://p-signal-test.smartvideogo.com',
                  websocketPath: '/iot-channel/connect', signalPingInterval: 20,
                  expirationTime: Date.now() + 1800000, iceServer: []};
    if (token === 'expired-ticket') data.expirationTime = 1;
    if (token === 'untrusted-signal') data.signalServer = 'wss://unexpected.example.invalid';
    if (token === 'bad-ticket') delete data.accessToken;
    return reply(res, 200, {result: 0, data});
  }
  return reply(res, 501, {result: -9999});
}).listen(process.env.PORT || 43861);
