"""Exercise Home's real authenticated aggregation and signed tenant boundary."""


def verify(api, owner, outsider, organization, other_organization):
    path = '/api/home/summary'
    api('GET', path, expected=(401, 403))
    summary = api('GET', path + f'?org_id={organization}', owner)
    if summary.get('org_id') != organization:
        raise RuntimeError('Home summary must retain the authenticated requested organization')
    organizations = {item['id'] for item in summary.get('orgs', [])}
    if organization not in organizations or other_organization in organizations:
        raise RuntimeError('Home organization projection is missing or crossed tenant boundaries')
    tasks = summary.get('widgets', {}).get('tasks_by_status', {})
    counts = tasks.get('data')
    if (tasks.get('status') != 'ok' or not isinstance(counts, dict)
            or not counts or any(type(value) is not int or value < 0 for value in counts.values())
            or sum(counts.values()) < 1):
        raise RuntimeError('Home must aggregate the real acceptance task through Pulse')
    api('GET', path + f'?org_id={other_organization}', owner, expected=(403,))
    api('GET', path + f'?org_id={organization}', outsider, expected=(403,))
    print('PASS Home real Pulse aggregation, anonymous denial and cross-tenant refusal', flush=True)
