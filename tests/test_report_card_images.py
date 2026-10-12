import base64
import hashlib
import json

import pytest

from playtrace.simulation_report import _attach_card_images


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5xkAAAAASUVORK5CYII=')


def manifest(tmp_path, **entry_changes):
    (tmp_path/'card.png').write_bytes(PNG)
    entry = {'path':'card.png','sha256':hashlib.sha256(PNG).hexdigest(), **entry_changes}
    value = {'snapshot_digest':'snapshot','cards':{'sample':{'name':'Sample card','front':entry,'back':entry}}}
    path=tmp_path/'manifest.json'
    path.write_text(json.dumps(value))
    return path


def test_images_embedded_once_per_trace_without_filesystem_links(tmp_path):
    traces=[{'snapshot_digest':'snapshot'}]
    _attach_card_images(traces,manifest(tmp_path))
    card=traces[0]['card_images']['sample']
    assert card['front']==card['back']=='data:image/png;base64,'+base64.b64encode(PNG).decode()
    assert 'path' not in card


@pytest.mark.parametrize('entry, digest, message', [
    ({},'other','different source snapshot'),
    ({'sha256':'stale'},'snapshot','fingerprint changed'),
    ({'path':'../outside.png'},'snapshot','escapes'),
])
def test_images_reject_stale_or_escaping_references(tmp_path,entry,digest,message):
    with pytest.raises(ValueError,match=message):
        _attach_card_images([{'snapshot_digest':digest}],manifest(tmp_path,**entry))


def test_images_reject_active_content(tmp_path):
    path=manifest(tmp_path)
    raw=b'<svg onload="alert(1)"></svg>'
    (tmp_path/'card.png').write_bytes(raw)
    value=json.loads(path.read_text())
    value['cards']['sample']['front']['sha256']=hashlib.sha256(raw).hexdigest()
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='PNG, JPEG or WebP'):
        _attach_card_images([{'snapshot_digest':'snapshot'}],path)
