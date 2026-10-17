// Hive's cold index is asynchronous: its documented building response asks
// the real frontend to retry. Every other server error remains a failure.
export function isPendingHiveIndex(application, request, headers, body) {
  return application === 'hive' && request.method === 'GET' && request.status === 503
    && /^\/hive\/api\/v1\/fleet\/(agent-cards|agents)\/$/.test(request.path)
    && headers['x-hive-cache'] === 'building' && headers['retry-after'] === '1'
    && body?.error === 'agents index rebuilding' && body?.retry === true
}
