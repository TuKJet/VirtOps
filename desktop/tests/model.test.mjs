import test from 'node:test';
import assert from 'node:assert/strict';
import { validPort, validResource, resultTab, terminalJob, completedLocalAction, isoEditions, sealedImage, mergeServiceResult } from '../src/model.ts';

test('port and resource validation rejects unsafe values', () => {
  assert.equal(validPort(1), true);
  assert.equal(validPort(65535), true);
  assert.equal(validPort(0), false);
  assert.equal(validPort(65536), false);
  assert.equal(validPort(8000.5), false);
  assert.equal(validResource(96), true);
  assert.equal(validResource(0), false);
});

test('job results route to the correct detail tab', () => {
  assert.equal(resultTab('publish'), 'services');
  assert.equal(resultTab('remove_service'), 'services');
  assert.equal(resultTab('logs'), 'logs');
  assert.equal(resultTab('list_files'), 'files');
  assert.equal(resultTab('inspect'), 'overview');
});

test('long jobs remain non-terminal until backend reports a final state', () => {
  assert.equal(terminalJob('queued'), false);
  assert.equal(terminalJob('running'), false);
  assert.equal(terminalJob('succeeded'), true);
  assert.equal(terminalJob('failed'), true);
  assert.equal(terminalJob('interrupted'), true);
});

test('image build only completes after a sealed image is reported', () => {
  assert.equal(completedLocalAction('build_image', { status: 'prepared' }), false);
  assert.equal(completedLocalAction('build_image', { status: 'awaiting_install' }), false);
  assert.equal(completedLocalAction('build_image', { status: 'imported' }), true);
  assert.equal(completedLocalAction('build_image', { status: 'ready' }), true);
});

test('engine ISO and image payloads use the real protocol shapes', () => {
  const inspect = { status: 'ok', path: 'D:\\Windows.iso', images: [{ index: 6, name: 'Windows 11 Pro', edition: 'Professional', architecture: 'x64', languages: ['zh-CN'] }] };
  assert.deepEqual(isoEditions(inspect), inspect.images);
  assert.deepEqual(isoEditions({ status: 'ok', path: 'D:\\Windows.iso', isoImages: inspect.images }), []);
  const build = { status: 'imported', image: { id: 'abc', name: 'base', path: 'D:\\base.vhdx', sha256: 'hash', virtualBytes: 64 } };
  assert.deepEqual(sealedImage(build), build.image);
  assert.equal(sealedImage({ status: 'imported', images: [build.image] }), undefined);
});

test('single-service publish results are reduced into the existing service list', () => {
  const previous = { services: [{ id: 'old', guest_port: 8000 }] };
  const published = { status: 'published', service: { id: 'new', guest_port: 8001 }, endpoint: 'http://127.0.0.1:18001' };
  const merged = mergeServiceResult(previous, published, 'publish');
  assert.deepEqual(merged.services, [previous.services[0], published.service]);
  assert.equal(mergeServiceResult(merged, { status: 'removed', service_id: 'new' }, 'remove_service').services.length, 1);
});
