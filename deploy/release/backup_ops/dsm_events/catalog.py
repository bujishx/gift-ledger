"""Reviewed program versions. No machine address or resource identity belongs here."""

# Includes the reviewed Container Manager UI programs over two MiB. Discovery
# and the immediately-before-mutation recheck must use the same bounded read.
VENDOR_FILE_LIMIT = 8 * 1024**2

# Release integrator appends only a reviewed, complete toolset here after the
# release sources have been frozen. Runtime/configuration cannot add versions.
RELEASE_VERSIONS = []

CORE_VERSIONS = {'810a122d07a9666c2e389e02cafee8f249c53e9cd86731c61593f735ac8b0a16': {'id': 'legacy-s011-v1',
                                                                      'schedule_authorized_keyword': False,
                                                                      'static_tools': {'backup_ops/backup_audit.py': '573fadac4dde35be3330d001510d6c1bbfd901db41ca0e381530d3312a4176b8',
                                                                                       'backup_ops/backup_export.py': '9db5099b7edeb26a1ed4f24ec54088da88674b78a00117252dfb992dd2de03f2',
                                                                                       'backup_ops/backup_ops.py': '810a122d07a9666c2e389e02cafee8f249c53e9cd86731c61593f735ac8b0a16',
                                                                                       'backup_ops/backup_store.py': 'd2199decf780d4cbad2f125610a36ed92e870053cedc0fde82c07f0ae7a599b1',
                                                                                       'backup_ops/backup_worker.py': 'a713bff2635487a0a2c0485881bee527d78dd834815537ec5595fd81f97e8a4a',
                                                                                       'backup_ops/preview_policy.py': '896524b69f7f7044a9e966b58662f3893909ba10b1a6412647ced19a80db9f88',
                                                                                       'backup_ops/resource_meter.py': '769c0a257291c07935254f67483096c04d79a45f95d8b4f0108c80364f653718',
                                                                                       'gui/initialize.py': 'f3578939c3cae677cdf4fd3837c82baf4fdf0d9b25658d92a8f3c0a1289a52a3',
                                                                                       'gui/prepare.py': 'f04d7871c19b78dcb719280c9f7a867b42dd31eb50d776eec3ce1456393aaed9',
                                                                                       'gui_v2/certificate_renewal.py': '1d2e0deed288a5a82621d434af5e36e69673b7701d68fea789084e3c20f58730',
                                                                                       'gui_v2/key_transfer.py': '7ecaadb701bac4b5052ca30e42b939a5a4ffcc440d4cd6cd2d73a18785c3aded',
                                                                                       'gui_v2/maintenance.py': '6d3291e6742896f1c9b450dd06be054de69da3ea5e6163ecb56015e729831d9c',
                                                                                       'gui_v2/prepare_volumes.py': '1602ed5b4d00937d1840a90bb1003a4995742d67952de0a12a5d74a96c6a42c6'},
                                                                      'dynamic_tools': []},
 'bda05ed75c695c7c19ba26e47d7430a410f26736eb9c05c0425835103b90e478': {'id': 'portable-s011-v1',
                                                                      'schedule_authorized_keyword': True,
                                                                      'static_tools': {'backup_ops/backup_audit.py': '573fadac4dde35be3330d001510d6c1bbfd901db41ca0e381530d3312a4176b8',
                                                                                       'backup_ops/backup_export.py': '9db5099b7edeb26a1ed4f24ec54088da88674b78a00117252dfb992dd2de03f2',
                                                                                       'backup_ops/backup_ops.py': 'bda05ed75c695c7c19ba26e47d7430a410f26736eb9c05c0425835103b90e478',
                                                                                       'backup_ops/backup_store.py': 'd2199decf780d4cbad2f125610a36ed92e870053cedc0fde82c07f0ae7a599b1',
                                                                                       'backup_ops/backup_worker.py': 'b426bb59666183c0064f9eb82e99812e013bfa41f42eac8f97f8080b1f9f3d37',
                                                                                       'backup_ops/preview_policy.py': '896524b69f7f7044a9e966b58662f3893909ba10b1a6412647ced19a80db9f88',
                                                                                       'backup_ops/resource_meter.py': '769c0a257291c07935254f67483096c04d79a45f95d8b4f0108c80364f653718',
                                                                                       'backup_ops/windows_path_budget.py': '3513c6e55845358da6032691638f9278e3a093069aca8a052d2b7152b07be4e0',
                                                                                       'gui/initialize.py': '5b4aefb6afbbea60a1836b2237acb8d2891f12bb3d0ba22361fff7ab900f27da',
                                                                                       'gui/prepare.py': 'd4d43cfa8c19929b424ed52dab332c870cf7931ec86687de8a9cd804080a08d9',
                                                                                       'gui_v2/certificate_renewal.py': '1d2e0deed288a5a82621d434af5e36e69673b7701d68fea789084e3c20f58730',
                                                                                       'gui_v2/key_transfer.py': 'c28a19617698ea60425f45cf6ad99bcc06b29fc1244aaed4e97c922b19e4d23f',
                                                                                       'gui_v2/maintenance.py': 'b1c6da4d9533e7284d08014934ede64dc1695cc53eae3bbea140f17a37dac47a',
                                                                                       'gui_v2/portable_dsm_profile.py': 'b3d29f5525a12b2e5cb3e0ac0a2467cb978f5edf114dcf39e66d36babfeeff97',
                                                                                       'gui_v2/portable_dsm_target.py': 'f14c00bfeeadc64754dad7ccbc0b81174f61e0befc8173c71e4087f119a75dc0',
                                                                                       'gui_v2/prepare_volumes.py': '1602ed5b4d00937d1840a90bb1003a4995742d67952de0a12a5d74a96c6a42c6'},
                                                                      'dynamic_tools': ['gui_v2/PORTABLE-DSM.json']}}

# D01 R13 retained the same backup coordinator, but fixed its generated-target
# adapters and maintenance compatibility. Match the complete reviewed toolset;
# do not independently allow arbitrary old/new combinations of these members.
# Source freeze: e64caea675bc72e82d26ee78842d827c75300b3d.
PORTABLE_CORE = 'bda05ed75c695c7c19ba26e47d7430a410f26736eb9c05c0425835103b90e478'
TOOLSET_VARIANTS = {PORTABLE_CORE: [{
    **CORE_VERSIONS[PORTABLE_CORE],
    'toolset_revision': 'D01-R13',
    'static_tools': {**CORE_VERSIONS[PORTABLE_CORE]['static_tools'],
        'gui_v2/maintenance.py': 'de08b2d509f1ed8b4c954a288ddcc96905699afac785f23973ab3dfdf4880bd7',
        'gui_v2/portable_dsm_profile.py': 'd4f174670305e83b5ff06065af1ff44c76cf4fbc71b942c5f08de38873b2d8fe',
        'gui_v2/portable_dsm_target.py': 'effd42a0b766eb5c113cc07f09ce8ef8ee78816d36db503a2f6b58d5b30aa9d9'},
}]}

# The D03 coordinator adds only the reviewed Worker extension points. This
# complete combination was independently checked and then exercised by the
# owner's successful DSM manual backup on 2026-11-04. The generated contract
# remains a dynamic, privately registered hash, not a machine-specific default.
REVIEWED_C4EB6_CORE = 'c4eb6ae53dbca8624088af4e1f1b1e27c5ffd1124ada11245a2b9d6c6d4a8f85'
RELEASE_VERSIONS.append({**TOOLSET_VARIANTS[PORTABLE_CORE][0],
    'id': 'portable-s011-dsm8-manual-c4eb6-v1', 'toolset_revision': 'D03-reviewed-c4eb6',
    'static_tools': {**TOOLSET_VARIANTS[PORTABLE_CORE][0]['static_tools'],
                     'backup_ops/backup_ops.py': REVIEWED_C4EB6_CORE}})

# Select one complete vendor file set. Separate administrative tools keep
# their original 1543 scope until their own lifecycle operations are tested.
BASE_CM_BUILD = '24.0.2-1543'
CONTAINER_MANAGER_BUILDS = {
    BASE_CM_BUILD: {
        'tool/docker_event_watcherd': '63654e41b1c2d871bdc8af2ad31777c177a720162592d0b018d0b2e3b35be777',
        'webapi/container/SYNO.Docker.Container.so': 'e8ad4940bcb89bec6311c95418fe11d25ff8bb20d5c31929324dff60b695072c',
        'ui/MainVue.js': 'b8769d427eafd1d98f7fdff9486043f90521a0ad85295ea85d841e1492427366',
        'webapi/SYNO.Docker.lib': '929a3dbb16a62881fb694c3f5b2f70979b117ac773f502c02adc89e3ae11d7a5',
    },
    '24.0.2-1606': {
        'tool/docker_event_watcherd': '31e7877e1362d8f118e621e021b62f44259135fe55171e27089613398dfa86e3',
        'webapi/container/SYNO.Docker.Container.so': 'e8ad4940bcb89bec6311c95418fe11d25ff8bb20d5c31929324dff60b695072c',
        'ui/MainVue.js': '572445f86dad09bbdb4b3abc13897ceece973fb0b7151292ed7e535d537e1dcb',
        'webapi/SYNO.Docker.lib': '929a3dbb16a62881fb694c3f5b2f70979b117ac773f502c02adc89e3ae11d7a5',
    },
}

# Complete candidate toolset reviewed with focused D11/D12 checks; no runtime allowlist extension.
RELEASE_VERSIONS.append({'dynamic_tools': ['gui_v2/PORTABLE-DSM.json'],
 'id': 'portable-s011-d11d12-r15-v1',
 'schedule_authorized_keyword': True,
 'static_tools': {'backup_ops/backup_audit.py': '573fadac4dde35be3330d001510d6c1bbfd901db41ca0e381530d3312a4176b8',
                  'backup_ops/backup_export.py': '9db5099b7edeb26a1ed4f24ec54088da88674b78a00117252dfb992dd2de03f2',
                  'backup_ops/backup_ops.py': '8140bb393827bc91037614e789be038750a4198b63245ef5222a1de7d0b75688',
                  'backup_ops/backup_store.py': 'd2199decf780d4cbad2f125610a36ed92e870053cedc0fde82c07f0ae7a599b1',
                  'backup_ops/backup_worker.py': 'b426bb59666183c0064f9eb82e99812e013bfa41f42eac8f97f8080b1f9f3d37',
                  'backup_ops/preview_policy.py': '896524b69f7f7044a9e966b58662f3893909ba10b1a6412647ced19a80db9f88',
                  'backup_ops/resource_meter.py': '769c0a257291c07935254f67483096c04d79a45f95d8b4f0108c80364f653718',
                  'backup_ops/windows_path_budget.py': '3513c6e55845358da6032691638f9278e3a093069aca8a052d2b7152b07be4e0',
                  'gui/initialize.py': '5b4aefb6afbbea60a1836b2237acb8d2891f12bb3d0ba22361fff7ab900f27da',
                  'gui/prepare.py': 'd4d43cfa8c19929b424ed52dab332c870cf7931ec86687de8a9cd804080a08d9',
                  'gui_v2/access_origins.py': 'ab14eea0121ebfe2eb9709ed0cb55547dbae3a4b8fc7f903d2075692208006e0',
                  'gui_v2/certificate_renewal.py': '1d2e0deed288a5a82621d434af5e36e69673b7701d68fea789084e3c20f58730',
                  'gui_v2/key_transfer.py': 'c28a19617698ea60425f45cf6ad99bcc06b29fc1244aaed4e97c922b19e4d23f',
                  'gui_v2/maintenance.py': 'de08b2d509f1ed8b4c954a288ddcc96905699afac785f23973ab3dfdf4880bd7',
                  'gui_v2/portable_dsm_profile.py': '8c9d1ba8275465225cf44d0da8ac4a35caef2771c435546f6e042e99837348a0',
                  'gui_v2/portable_dsm_target.py': 'd4334ad7307a22f236179b6e94bc037a90117c2b5d922e131a03dabb71322975',
                  'gui_v2/prepare_volumes.py': '1602ed5b4d00937d1840a90bb1003a4995742d67952de0a12a5d74a96c6a42c6'},
 'toolset_revision': 'D11-D12-R15'})

# V3 dependency closure added before any candidate enrollment.
RELEASE_VERSIONS.append({'dynamic_tools': ['gui_v2/PORTABLE-DSM.json'],
 'id': 'portable-s011-d11d12-r15-v2',
 'schedule_authorized_keyword': True,
 'static_tools': {'backup_ops/backup_audit.py': '573fadac4dde35be3330d001510d6c1bbfd901db41ca0e381530d3312a4176b8',
                  'backup_ops/backup_export.py': '9db5099b7edeb26a1ed4f24ec54088da88674b78a00117252dfb992dd2de03f2',
                  'backup_ops/backup_ops.py': '2e05cdf4b0764c55ba06a7a25b7f660f2cf209f763bba70306b60ef14a1e0bee',
                  'backup_ops/backup_store.py': 'd2199decf780d4cbad2f125610a36ed92e870053cedc0fde82c07f0ae7a599b1',
                  'backup_ops/backup_worker.py': 'b426bb59666183c0064f9eb82e99812e013bfa41f42eac8f97f8080b1f9f3d37',
                  'backup_ops/preview_policy.py': '896524b69f7f7044a9e966b58662f3893909ba10b1a6412647ced19a80db9f88',
                  'backup_ops/resource_meter.py': '769c0a257291c07935254f67483096c04d79a45f95d8b4f0108c80364f653718',
                  'backup_ops/windows_path_budget.py': '3513c6e55845358da6032691638f9278e3a093069aca8a052d2b7152b07be4e0',
                  'gui/initialize.py': '5b4aefb6afbbea60a1836b2237acb8d2891f12bb3d0ba22361fff7ab900f27da',
                  'gui/prepare.py': 'd4d43cfa8c19929b424ed52dab332c870cf7931ec86687de8a9cd804080a08d9',
                  'gui_v2/access_origins.py': 'ab14eea0121ebfe2eb9709ed0cb55547dbae3a4b8fc7f903d2075692208006e0',
                  'gui_v2/certificate_renewal.py': '1d2e0deed288a5a82621d434af5e36e69673b7701d68fea789084e3c20f58730',
                  'gui_v2/key_transfer.py': 'c28a19617698ea60425f45cf6ad99bcc06b29fc1244aaed4e97c922b19e4d23f',
                  'gui_v2/maintenance.py': 'de08b2d509f1ed8b4c954a288ddcc96905699afac785f23973ab3dfdf4880bd7',
                  'gui_v2/portable_dsm_profile.py': '8c9d1ba8275465225cf44d0da8ac4a35caef2771c435546f6e042e99837348a0',
                  'gui_v2/portable_dsm_target.py': 'd4334ad7307a22f236179b6e94bc037a90117c2b5d922e131a03dabb71322975',
                  'gui_v2/prepare_volumes.py': '1602ed5b4d00937d1840a90bb1003a4995742d67952de0a12a5d74a96c6a42c6'},
 'toolset_revision': 'D11-D12-R15'})


# The final V3 access-maintenance profile marker pins five modules instead of
# two. Only backup_ops.py changed from v2; every DSM tool hash below was checked
# independently. Preserve v1/v2 for already-installed candidates. This exact
# 17-member static set plus the privately pinned target is the only new match.
RELEASE_VERSIONS.append({'dynamic_tools': ['gui_v2/PORTABLE-DSM.json'],
 'id': 'portable-s011-d11d12-r15-v3',
 'schedule_authorized_keyword': True,
 'static_tools': {'backup_ops/backup_audit.py': '573fadac4dde35be3330d001510d6c1bbfd901db41ca0e381530d3312a4176b8',
                  'backup_ops/backup_export.py': '9db5099b7edeb26a1ed4f24ec54088da88674b78a00117252dfb992dd2de03f2',
                  'backup_ops/backup_ops.py': '4c93b348372757ad1be3bce52b569efdb67d68d5e256b5d0749dae3c7b3038b5',
                  'backup_ops/backup_store.py': 'd2199decf780d4cbad2f125610a36ed92e870053cedc0fde82c07f0ae7a599b1',
                  'backup_ops/backup_worker.py': 'b426bb59666183c0064f9eb82e99812e013bfa41f42eac8f97f8080b1f9f3d37',
                  'backup_ops/preview_policy.py': '896524b69f7f7044a9e966b58662f3893909ba10b1a6412647ced19a80db9f88',
                  'backup_ops/resource_meter.py': '769c0a257291c07935254f67483096c04d79a45f95d8b4f0108c80364f653718',
                  'backup_ops/windows_path_budget.py': '3513c6e55845358da6032691638f9278e3a093069aca8a052d2b7152b07be4e0',
                  'gui/initialize.py': '5b4aefb6afbbea60a1836b2237acb8d2891f12bb3d0ba22361fff7ab900f27da',
                  'gui/prepare.py': 'd4d43cfa8c19929b424ed52dab332c870cf7931ec86687de8a9cd804080a08d9',
                  'gui_v2/access_origins.py': 'ab14eea0121ebfe2eb9709ed0cb55547dbae3a4b8fc7f903d2075692208006e0',
                  'gui_v2/certificate_renewal.py': '1d2e0deed288a5a82621d434af5e36e69673b7701d68fea789084e3c20f58730',
                  'gui_v2/key_transfer.py': 'c28a19617698ea60425f45cf6ad99bcc06b29fc1244aaed4e97c922b19e4d23f',
                  'gui_v2/maintenance.py': 'de08b2d509f1ed8b4c954a288ddcc96905699afac785f23973ab3dfdf4880bd7',
                  'gui_v2/portable_dsm_profile.py': '8c9d1ba8275465225cf44d0da8ac4a35caef2771c435546f6e042e99837348a0',
                  'gui_v2/portable_dsm_target.py': 'd4334ad7307a22f236179b6e94bc037a90117c2b5d922e131a03dabb71322975',
                  'gui_v2/prepare_volumes.py': '1602ed5b4d00937d1840a90bb1003a4995742d67952de0a12a5d74a96c6a42c6'},
 'toolset_revision': 'D11-D12-R15'})


# D13 installation notes only; lifecycle AST unchanged from v3.
# Exact current DSM tool combination; retain every prior installed candidate.
RELEASE_VERSIONS.append({'dynamic_tools': ['gui_v2/PORTABLE-DSM.json'],
 'id': 'portable-s011-d11d12-r15-v4',
 'schedule_authorized_keyword': True,
 'static_tools': {'backup_ops/backup_audit.py': '573fadac4dde35be3330d001510d6c1bbfd901db41ca0e381530d3312a4176b8',
                  'backup_ops/backup_export.py': '9db5099b7edeb26a1ed4f24ec54088da88674b78a00117252dfb992dd2de03f2',
                  'backup_ops/backup_ops.py': '5bcf2af78fbd5ed01d2b59c3b111764532884f082ff43624526ff3071d255d55',
                  'backup_ops/backup_store.py': 'd2199decf780d4cbad2f125610a36ed92e870053cedc0fde82c07f0ae7a599b1',
                  'backup_ops/backup_worker.py': 'b426bb59666183c0064f9eb82e99812e013bfa41f42eac8f97f8080b1f9f3d37',
                  'backup_ops/preview_policy.py': '896524b69f7f7044a9e966b58662f3893909ba10b1a6412647ced19a80db9f88',
                  'backup_ops/resource_meter.py': '769c0a257291c07935254f67483096c04d79a45f95d8b4f0108c80364f653718',
                  'backup_ops/windows_path_budget.py': '3513c6e55845358da6032691638f9278e3a093069aca8a052d2b7152b07be4e0',
                  'gui/initialize.py': '5b4aefb6afbbea60a1836b2237acb8d2891f12bb3d0ba22361fff7ab900f27da',
                  'gui/prepare.py': 'd4d43cfa8c19929b424ed52dab332c870cf7931ec86687de8a9cd804080a08d9',
                  'gui_v2/access_origins.py': 'ab14eea0121ebfe2eb9709ed0cb55547dbae3a4b8fc7f903d2075692208006e0',
                  'gui_v2/certificate_renewal.py': '1d2e0deed288a5a82621d434af5e36e69673b7701d68fea789084e3c20f58730',
                  'gui_v2/key_transfer.py': 'c28a19617698ea60425f45cf6ad99bcc06b29fc1244aaed4e97c922b19e4d23f',
                  'gui_v2/maintenance.py': 'de08b2d509f1ed8b4c954a288ddcc96905699afac785f23973ab3dfdf4880bd7',
                  'gui_v2/portable_dsm_profile.py': '8c9d1ba8275465225cf44d0da8ac4a35caef2771c435546f6e042e99837348a0',
                  'gui_v2/portable_dsm_target.py': 'd4334ad7307a22f236179b6e94bc037a90117c2b5d922e131a03dabb71322975',
                  'gui_v2/prepare_volumes.py': '1602ed5b4d00937d1840a90bb1003a4995742d67952de0a12a5d74a96c6a42c6'},
 'toolset_revision': 'D11-D12-R15'})


# D14 user instructions only. All runtime commands/gates and the other
# 16 static tools remain as reviewed R15-v4; preserve every installed version.
RELEASE_VERSIONS.append({'dynamic_tools': ['gui_v2/PORTABLE-DSM.json'],
 'id': 'portable-s011-d14-r16-v1',
 'schedule_authorized_keyword': True,
 'static_tools': {'backup_ops/backup_audit.py': '573fadac4dde35be3330d001510d6c1bbfd901db41ca0e381530d3312a4176b8',
                  'backup_ops/backup_export.py': '9db5099b7edeb26a1ed4f24ec54088da88674b78a00117252dfb992dd2de03f2',
                  'backup_ops/backup_ops.py': 'ae9141996c441f48245de25f7aaf08486fb386a88f9604f80bc9b94f0bf00b33',
                  'backup_ops/backup_store.py': 'd2199decf780d4cbad2f125610a36ed92e870053cedc0fde82c07f0ae7a599b1',
                  'backup_ops/backup_worker.py': 'b426bb59666183c0064f9eb82e99812e013bfa41f42eac8f97f8080b1f9f3d37',
                  'backup_ops/preview_policy.py': '896524b69f7f7044a9e966b58662f3893909ba10b1a6412647ced19a80db9f88',
                  'backup_ops/resource_meter.py': '769c0a257291c07935254f67483096c04d79a45f95d8b4f0108c80364f653718',
                  'backup_ops/windows_path_budget.py': '3513c6e55845358da6032691638f9278e3a093069aca8a052d2b7152b07be4e0',
                  'gui/initialize.py': '5b4aefb6afbbea60a1836b2237acb8d2891f12bb3d0ba22361fff7ab900f27da',
                  'gui/prepare.py': 'd4d43cfa8c19929b424ed52dab332c870cf7931ec86687de8a9cd804080a08d9',
                  'gui_v2/access_origins.py': 'ab14eea0121ebfe2eb9709ed0cb55547dbae3a4b8fc7f903d2075692208006e0',
                  'gui_v2/certificate_renewal.py': '1d2e0deed288a5a82621d434af5e36e69673b7701d68fea789084e3c20f58730',
                  'gui_v2/key_transfer.py': 'c28a19617698ea60425f45cf6ad99bcc06b29fc1244aaed4e97c922b19e4d23f',
                  'gui_v2/maintenance.py': 'de08b2d509f1ed8b4c954a288ddcc96905699afac785f23973ab3dfdf4880bd7',
                  'gui_v2/portable_dsm_profile.py': '8c9d1ba8275465225cf44d0da8ac4a35caef2771c435546f6e042e99837348a0',
                  'gui_v2/portable_dsm_target.py': 'd4334ad7307a22f236179b6e94bc037a90117c2b5d922e131a03dabb71322975',
                  'gui_v2/prepare_volumes.py': '1602ed5b4d00937d1840a90bb1003a4995742d67952de0a12a5d74a96c6a42c6'},
 'toolset_revision': 'D14-R16'})
