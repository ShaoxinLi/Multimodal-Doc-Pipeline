import os
import shutil
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from PIL import Image

from doc_pipeline.contracts.core import FileRef, InputFile
from doc_pipeline.parsers.text.parser import parse_text_document
from doc_pipeline.pipeline import process_file
from tests.parsers.text.support import parse


@pytest.mark.parametrize(('suffix', 'text'), [
    ('md', '![plot](assets/plot.png)'),
    ('markdown', '![plot](assets/plot.png)'),
    ('rst', '.. image:: assets/plot.png\n'),
    ('tex', r'\begin{figure}\includegraphics{assets/plot.png}\end{figure}'),
    ('latex', r'\begin{figure}\includegraphics{assets/plot.png}\end{figure}'),
])
def test_relocated_document_loads_adjacent_image_without_mapping(tmp_path, monkeypatch, suffix, text):
    original = tmp_path / 'original'
    (original / 'assets').mkdir(parents=True)
    Image.new('RGB', (4, 3), 'red').save(original / 'assets/plot.png')
    data = (original / 'assets/plot.png').read_bytes()
    (original / f'guide.{suffix}').write_text(text)
    package = tmp_path / 'moved'
    shutil.move(original, package)
    monkeypatch.chdir(tmp_path.parent)

    def reject_request(request):
        pytest.fail(f'Unexpected request: {request.url}')

    output = tmp_path / 'output'
    with httpx.Client(transport=httpx.MockTransport(reject_request)) as client:
        result = process_file(package / f'guide.{suffix}', output, client=client)
    archived = f'assets/{sha256(data).hexdigest()}.png'
    assert (output / archived).read_bytes() == data
    assert archived in (output / 'content.md').read_text()
    assert result['chunk_count'] > 0
    assert not any('snapshot' in d or 'blocked_reference' in d for d in result['diagnostics'])


def test_local_reference_decodes_path_and_ignores_query_and_fragment(tmp_path):
    (tmp_path / 'assets').mkdir()
    Image.new('RGB', (3, 2)).save(tmp_path / 'assets/my plot.png')
    parsed = parse(tmp_path, 'guide.md', 'A plot: ![plot](assets/my%20plot.png?v=1#figure)')
    assert parsed.inline_assets[0].resolution == 'archived'


def test_absent_local_image_is_missing_not_invalid(tmp_path):
    parsed = parse(tmp_path, 'guide.md', 'A plot: ![plot](absent.png)')
    assert parsed.inline_assets[0].resolution == 'missing_snapshot'
    assert parsed.diagnostics == ['missing_snapshot: absent.png']


@pytest.mark.parametrize('valid_override', [True, False])
def test_explicit_mapping_is_authoritative(tmp_path, valid_override):
    Image.new('RGB', (3, 2), 'red').save(tmp_path / 'plot.png')
    override = tmp_path / 'override.png'
    if valid_override:
        Image.new('RGB', (2, 3), 'blue').save(override)
    else:
        override.write_bytes(b'not an image')
    mapped = tmp_path / 'mapped.png'
    mapped.symlink_to(override)
    parsed = parse(tmp_path, 'guide.md', 'A plot: ![plot](plot.png)', assets={'plot.png': mapped})
    asset = parsed.inline_assets[0]
    assert asset.resolution == ('archived' if valid_override else 'missing_snapshot')
    if valid_override:
        assert asset.asset.sha256 == sha256(override.read_bytes()).hexdigest()
    else:
        assert asset.asset is None


@pytest.mark.parametrize('reference', ['../outside.png', '%2e%2e/outside.png', '/outside.png',
                                      'file:///outside.png', 'link.png', 'linked/outside.png'])
def test_automatic_images_cannot_escape_document_directory(tmp_path, reference):
    Image.new('RGB', (3, 2)).save(tmp_path / 'outside.png')
    source = tmp_path / 'document'
    source.mkdir()
    (source / 'link.png').symlink_to(tmp_path / 'outside.png')
    (source / 'linked').symlink_to(tmp_path, target_is_directory=True)
    parsed = parse(source, 'guide.md', f'![plot]({reference})')
    assert parsed.inline_assets[0].resolution == 'blocked_reference'
    assert not (source / 'assets').exists()


def test_internal_symlink_and_external_url_have_distinct_resolution(tmp_path):
    Image.new('RGB', (3, 2)).save(tmp_path / 'plot.png')
    (tmp_path / 'link.png').symlink_to(tmp_path / 'plot.png')
    parsed = parse(tmp_path, 'guide.md', '![local](link.png) ![remote](https://example.org/plot.png)')
    assert [asset.resolution for asset in parsed.inline_assets] == ['archived', 'external_unarchived']


@pytest.mark.parametrize('kind', ['directory', 'fifo'])
def test_nonregular_local_image_is_rejected_without_reading(tmp_path, kind):
    target = tmp_path / 'plot.png'
    if kind == 'directory':
        target.mkdir()
    else:
        os.mkfifo(target)
    parsed = parse(tmp_path, 'guide.md', 'A plot: ![plot](plot.png)')
    assert parsed.inline_assets[0].asset is None
    assert not (tmp_path / 'assets').exists()


@pytest.mark.skipif(not Path('/dev/fd').is_dir(), reason='Requires an open-file descriptor inventory')
def test_rejected_directories_do_not_leak_file_descriptors(tmp_path):
    (tmp_path / 'directory.png').mkdir()
    descriptors = len(list(Path('/dev/fd').iterdir()))
    for _ in range(30):
        parse(tmp_path, 'guide.md', '![plot](directory.png)')
    assert len(list(Path('/dev/fd').iterdir())) == descriptors


@pytest.mark.parametrize('excess', [0, 1])
def test_local_image_byte_limit(tmp_path, excess):
    Image.new('RGB', (3, 2)).save(tmp_path / 'plot.png')
    size = (tmp_path / 'plot.png').stat().st_size
    source = tmp_path / 'guide.md'
    source.write_text('A plot: ![plot](plot.png)')
    parsed = parse_text_document(InputFile(path=source), FileRef(path=str(source), sha256='0' * 64),
                                 tmp_path / 'out', max_asset_bytes=size - excess)
    assert parsed.inline_assets[0].resolution == ('missing_snapshot' if excess else 'archived')
    assert ('asset_too_large: plot.png' in parsed.diagnostics) == bool(excess)


def test_automatic_images_do_not_read_output_subtree(tmp_path):
    output = tmp_path / 'out'
    output.mkdir()
    Image.new('RGB', (3, 2)).save(output / 'plot.png')
    source = tmp_path / 'guide.md'
    source.write_text('![plot](out/plot.png)')
    parsed = parse_text_document(InputFile(path=source), FileRef(path=str(source), sha256='0' * 64), output)
    assert parsed.inline_assets[0].resolution == 'blocked_reference'
