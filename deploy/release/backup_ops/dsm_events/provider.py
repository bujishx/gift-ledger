"""Per-target planned stops through a verified Container Manager event consumer.

Caller owns the core lock, a fresh private journal and explicit start policy.
No default host, date, CLI, daemon, global notification change or retry.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

from . import catalog
from .target import VendorFileMismatch

def observer_source(template, config):
    if template.count(b'CONFIG = None') != 1:
        raise ValueError('observer_template_changed')
    assignment = ('CONFIG = ' + ascii(config)).encode('ascii')
    return b'#!/usr/bin/python3 -I\n' + template.replace(b'CONFIG = None', assignment)


class EventProvider:
    def __init__(self, driver, profile, private, helper, hook, hook_raw, base, files,
                 save, *, run_label, start_allowed, clock=time.time_ns,
                 monotonic=time.monotonic_ns, sleep=time.sleep, apply=None):
        self.driver, self.profile, self.private = driver, profile, Path(private)
        self.h, self.hook, self.hook_raw, self.base, self.files = helper, hook, hook_raw, base, files
        self.save = save
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.apply = apply or self.apply_API
        helper.require(re.fullmatch(r'gl-events-[0-9a-f]{32}',run_label),
                       'event_run_label_invalid')
        self.run_label = run_label
        helper.require(callable(start_allowed), 'explicit_start_policy_required')
        self.start_allowed = start_allowed
        self.subjects = {}
        self.retirement_attempted = False
        self.record = {'format':'gift-ledger-s1-full-backup-events-v1','subjects':{},
                       'notification_settings_changed':False,'vendor_original_files_modified':False,
                       'marker_containers_started':False,'retired':False,'cleanup_errors':[]}
        helper.require(profile['project'] == base.PROJECT and profile['engine_id'] == base.ENGINE
                       and profile['instance_id'] == base.INSTANCE, 'exact_target_provider_required')
        for role in ('web','api','db'):
            helper.require(profile['containers'][role]['id'] == base.IDS[role]
                           and profile['containers'][role]['name'] == base.PROJECT+'-'+role+'-1', 'service_binding_changed')

    def _save(self):
        self.save(self.record)

    def fresh(self):
        self.h.require(self.start_allowed(datetime.now(timezone.utc)), 'backup_start_policy_refused')

    def epoch(self, subject):
        return self.h.active_epoch(self.hook,subject['config']['epoch'])

    def state(self, role):
        return self.base.checked_state(self.driver,role)

    def apply_API(self, action, role):
        self.h.require(action in ('stop','start') and role in ('web','api'), 'only_bound_Web_API_allowed')
        name=self.profile['containers'][role]['name']
        argv=[str(self.base.API),'--exec','api=SYNO.Docker.Container','version=1','method='+action,'name='+json.dumps(name)]
        try:
            result=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                  encoding='utf-8',timeout=45,check=False)
        except subprocess.TimeoutExpired:
            raise ValueError('DSM_API_timeout_observe_no_retry') from None
        return self.base.parse_ack(result.stdout,result.returncode)

    def prepare(self,binding):
        self.h.require(not self.subjects,'event_provider_already_prepared')
        self.fresh();self.base.validate_package()
        self.h.check_other_operations(self.driver,self.base)
        self.h.require(self.h.parse(self.driver.call('info','--format','{{json .ID}}'))==self.base.ENGINE,'wrong_provider_engine')
        self.h.checked_directory(self.private)
        self.h.require(stat.S_IMODE(self.private.stat().st_mode)==0o700,'private_provider_mode_invalid')
        for path, expected in self.h.VENDOR_PINS.items():
            observed = hashlib.sha256(self.h.read_regular(path, catalog.VENDOR_FILE_LIMIT)).hexdigest()
            if observed != expected:
                package = self.base.package
                pins = catalog.CONTAINER_MANAGER_BUILDS[package.build]
                changed = {name: observed for name in pins if package.target/name == path}
                raise VendorFileMismatch('vendor_program_changed', changed, {package.build: pins})
        for role in ('web','api','db'):
            self.h.require(binding['containers'][role]==self.profile['containers'][role],'provider_binding_changed')
        for role in ('web','api'):
            self.base.checked_registration(role)
        epoch=self.h.active_epoch(self.hook)
        image=self.h.parse(self.driver.call('inspect','--format','{{json .Image}}',self.base.IDS['web'],timeout=15))
        info=self.h.parse(self.driver.call('image','inspect','--format',self.h.IMAGE_FORMAT,image,timeout=15))
        self.h.require(re.fullmatch('sha256:[0-9a-f]{64}',str(image)) and info.get('id')==image
                       and info.get('volumes') in (None,{}),'marker_image_invalid')
        root=self.h.VAR_ROOT.resolve(strict=True)
        self.h.require(root in self.h.ALLOWED_VAR_ROOTS,'package_var_location_changed')
        self.h.checked_directory(root);directory=root/'hook/event';self.h.checked_directory(directory)
        for role in ('web','api'):
            if not binding['original'][role]['running']:continue
            expected=self.profile['containers'][role]
            state=self.state(role)
            if not state['running'] and binding.get('recovering') is True:
                continue  # Already stopped in the retained, core-owned session.
            self.h.require(state['running'] and state['health']=='healthy','service_not_healthy_before_channel')
            profile_path=self.h.PROFILE_ROOT/(expected['name']+'.config')
            self.h.require(self.hook.enabled_value(profile_path) is True,'running_service_enabled_flag_required')
            nonce=uuid.uuid4().hex;started=self.clock()
            receipts=self.private/('events-'+role);receipts.mkdir(mode=0o700)
            config={'epoch':epoch,'nonce':nonce,'not_before_ns':started,'deadline_ns':started+300_000_000_000,
                'web_id':expected['id'],'web_name':expected['name'],'label':self.h.LABEL,
                'profile_path':str(profile_path),'receipt_directory':str(receipts),
                'marker_names':{kind:self.run_label+'-'+role+'-'+kind+'-'+nonce[:12] for kind in ('initial','after')}}
            for kind in ('initial','after'):
                config['marker_names'][kind+'_destroy']=config['marker_names'][kind]
            target=directory/('zz-gift-ledger-'+self.run_label+'-'+role+'.py')
            self.h.require(not target.exists() and not target.is_symlink(),'observer_already_exists')
            raw=observer_source(self.hook_raw, config)
            subject={'config':config,'image':image,'observer_path':str(target),
                     'observer_sha256':hashlib.sha256(raw).hexdigest(),'observer_installed':False,'markers':{}}
            self.subjects[role]=subject;self.record['subjects'][role]=subject;self._save()
            fd=os.open(str(target),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o700)
            subject['observer_identity']=[os.fstat(fd).st_dev,os.fstat(fd).st_ino]
            with os.fdopen(fd,'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno());os.fchmod(stream.fileno(),0o755)
            subject['observer_installed']=True;self._save()
            marker=self.make_marker(role,'initial')
            subject['initial_receipt']=self.wait_receipt(role,'initial',marker['id'],marker['intent_ns'],marker['intent_mono'])
            self.retire_marker(role,'initial')
            subject['initial_channel_verified']=True;self._save()
        self.h.require(bool(self.subjects),'no_running_application_subject')
        return {'initial_channel_verified':True}

    def lookup(self,role,kind):
        subject=self.subjects[role];name=subject['config']['marker_names'][kind]
        raw=self.driver.call('ps','-a','--no-trunc','--format','{"id":{{json .ID}},"name":{{json .Names}}}',timeout=15)
        self.h.require(len(raw)<131072,'marker_list_limit')
        rows=[self.h.parse(line) for line in raw.splitlines() if line]
        self.h.require(len(rows)<=500,'marker_list_limit')
        found=[row['id'] for row in rows if row.get('name')==name]
        self.h.require(len(found)<=1,'marker_name_ambiguous')
        if not found:return None
        value=self.h.parse(self.driver.call('inspect','--format',self.h.MARKER_FORMAT,found[0],timeout=15))
        return self.h.verify_marker(value,name,subject['image'],subject['config']['nonce'],found[0])

    def make_marker(self,role,kind):
        subject=self.subjects[role];self.fresh();self.epoch(subject)
        self.h.require(kind in ('initial','after') and kind not in subject['markers'],'marker_already_attempted')
        self.h.require(self.clock()<=subject['config']['deadline_ns'],'observer_expired')
        self.h.require(self.lookup(role,kind) is None,'marker_name_exists')
        name=subject['config']['marker_names'][kind]
        p=self.h.PROFILE_ROOT/(name+'.config')
        self.h.require(not p.exists() and not p.is_symlink(),'marker_profile_exists_before_create')
        entry={'name':name,'intent_ns':self.clock(),'intent_mono':self.monotonic(),'create_attempted':True}
        subject['markers'][kind]=entry;self._save()
        command=('create','--name',name,'--pull','never','--network','none','--read-only','--cap-drop','ALL',
                 '--security-opt','no-new-privileges','--restart','no','--label',self.h.LABEL+'='+subject['config']['nonce'],
                 '--entrypoint','/bin/true',subject['image'])
        try:
            result=self.driver.call(*command,timeout=25).strip()
            self.h.require(re.fullmatch('[0-9a-f]{64}',result),'marker_create_reply_invalid')
            entry['id']=result;self.h.require(self.lookup(role,kind)==result,'marker_create_not_verified')
        finally:
            if 'id' not in entry:
                found=self.lookup(role,kind)
                if found:entry['id']=found
            self._save()
        return entry

    def wait_receipt(self,role,kind,identity,intent_ns,intent_mono,prior_mono=0):
        subject=self.subjects[role];until=self.monotonic()+30_000_000_000
        path=Path(subject['config']['receipt_directory'])/(kind+'.json')
        while True:
            self.epoch(subject)
            self.h.require(self.clock()<=subject['config']['deadline_ns'],'observer_expired')
            if path.exists() or path.is_symlink():
                return self.h.validate_receipt(self.h.parse(self.h.read_regular(path,8192)),kind,subject['config'],
                                               identity,intent_ns,intent_mono,prior_mono)
            self.h.require(self.monotonic()<until,'event_receipt_timeout')
            self.sleep(0.2)

    def stop(self,role,expected):
        self.h.require(role in self.subjects and self.subjects[role].get('initial_channel_verified'), 'event_channel_unverified')
        subject=self.subjects[role]
        self.h.require(expected==self.profile['containers'][role],'stop_binding_changed')
        self.fresh();self.epoch(subject)
        self.base.checked_registration(role)
        self.h.require(not subject.get('stop_intent'),'stop_already_attempted')
        self.h.require(self.state(role)['running'] and self.hook.enabled_value(Path(subject['config']['profile_path'])) is True,
                       'service_changed_before_planned_stop')
        subject['stop_intent']={'ns':self.clock(),'mono':self.monotonic()};self._save()
        self.h.require(self.apply('stop',role).get('success') is True,'DSM_stop_not_acknowledged')
        until=self.monotonic()+30_000_000_000
        while self.state(role)['running']:
            self.h.require(self.monotonic()<until,'stop_state_timeout');self.sleep(0.2)
        intent=subject['stop_intent']
        die=self.wait_receipt(role,'die',expected['id'],intent['ns'],intent['mono'])
        subject['die_receipt']=die;self._save()
        item=self.make_marker(role,'after')
        after=self.wait_receipt(role,'after',item['id'],item['intent_ns'],item['intent_mono'],die['observed_monotonic_ns'])
        self.epoch(subject)
        self.h.require(self.hook.enabled_value(Path(subject['config']['profile_path'])) is False,'stop_flag_changed_before_release')
        subject['after_receipt']=after;subject['event_barrier_verified']=True;self._save()
        return {'success':True,'event_barrier_verified':True}

    def start(self,role,expected):
        self.h.require(role in ('web','api') and expected==self.profile['containers'][role],'start_binding_changed')
        self.base.checked_registration(role)
        # Date/observer failures must not block the core's proven-safe recovery.
        # This method never chooses when to resume; the adapter requires proof.
        self.h.require(self.apply('start',role).get('success') is True,'DSM_start_not_acknowledged')
        return {'success':True}

    def retire_profile(self,role,kind):
        subject=self.subjects[role];item=subject['markers'][kind]
        self.h.require('id' in item and item.get('container_absent'),'marker_absence_not_proven')
        root=self.h.PROFILE_ROOT.resolve(strict=True)
        self.h.require(root==self.base.package.profiles,'package_profile_root_not_reviewed')
        identity=self.h.checked_directory(root);fd=self.h.open_directory(root)
        name=item['name']+'.config'
        try:
            before=self.files.snapshot_file(fd,name,self.h)
            item['profile_before']=before;self._save()
            if before is not None:
                self.h.require(item['intent_ns']-1_000_000_000 <= before['mtime_ns'] <= self.clock()+1_000_000_000,
                               'marker_profile_time_not_owned')
                self.h.require(self.lookup(role,kind) is None,'marker_reappeared')
                item['profile_remove_intent']=True;self._save()
                self.h.require(self.h.PROFILE_ROOT.resolve(strict=True)==root and self.h.checked_directory(root)==identity,
                               'profile_directory_changed')
                latest=self.files.snapshot_file(fd,name,self.h)
                self.h.require(latest is None or latest==before,'marker_profile_changed')
                if latest is not None:
                    try:os.unlink(name,dir_fd=fd)
                    except FileNotFoundError:pass
                    else:item['profile_removed']=True
            try:os.stat(name,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError:pass
            else:raise ValueError('marker_profile_remains')
            item['profile_absent']=True;self._save()
        finally:os.close(fd)

    def retire(self):
        if self.retirement_attempted:
            if self.record['retired']:
                for role,subject in self.subjects.items():
                    path=Path(subject['observer_path'])
                    self.h.require(not path.exists() and not path.is_symlink(),'retired_observer_reappeared')
                    for kind in subject['markers']:self.retire_marker(role,kind)
            return {'retired':self.record['retired'],'errors':list(self.record['cleanup_errors'])}
        self.retirement_attempted=True
        errors=[]
        for role,subject in self.subjects.items():
            # Keep the observer until destroy has been seen by the same serial
            # consumer. Its earlier create handler can then no longer publish
            # a late profile after the exact profile check/removal below.
            for kind,item in subject['markers'].items():
                try:
                    self.retire_marker(role,kind)
                except Exception as error:errors.append(role+'_'+kind+'_'+self.base.safe_code(error))
            if 'observer_identity' in subject:
                try:
                    path=Path(subject['observer_path']);info=path.lstat()
                    self.h.require([info.st_dev,info.st_ino]==subject['observer_identity'] and stat.S_ISREG(info.st_mode),
                                   'observer_identity_changed')
                    self.h.require(hashlib.sha256(self.h.read_regular(path)).hexdigest()==subject['observer_sha256'],
                                   'observer_content_changed')
                    subject['observer_remove_intent']=True;self._save();path.unlink()
                    subject['observer_removed']=True;self._save()
                except Exception as error:errors.append(role+'_observer_'+self.base.safe_code(error))
        self.record['cleanup_errors']=errors;self.record['retired']=not errors;self._save()
        return {'retired':not errors,'errors':errors}

    def retire_marker(self,role,kind):
        subject=self.subjects[role];item=subject['markers'][kind]
        if item.get('profile_absent'):
            self.h.require(self.lookup(role,kind) is None,'retired_marker_reappeared')
            path=self.h.PROFILE_ROOT/(item['name']+'.config')
            self.h.require(not path.exists() and not path.is_symlink(),'retired_profile_reappeared')
            return
        self.epoch(subject)
        found=self.lookup(role,kind)
        self.h.require('id' in item,'marker_create_outcome_unknown')
        self.h.require(not item.get('remove_intent'),'marker_removal_already_attempted')
        self.h.require(found==item['id'],'marker_absent_without_destroy_receipt')
        item['remove_intent']={'ns':self.clock(),'mono':self.monotonic()};self._save()
        self.driver.call('rm',found,timeout=25)
        self.h.require(self.lookup(role,kind) is None,'marker_still_present')
        intent=item['remove_intent']
        item['destroy_receipt']=self.wait_receipt(role,kind+'_destroy',item['id'],intent['ns'],intent['mono'])
        item['container_absent']=True;self._save()
        self.retire_profile(role,kind)
