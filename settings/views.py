from django.shortcuts import render, render_to_response
from django.template import RequestContext, loader
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core import serializers
from django.http import HttpResponse
from proxy.models import *
from main.models import *
from lazy_balancer.views import is_auth
from nginx.views import reload_config
from .models import system_settings, sync_status
from datetime import datetime
from nginx.views import *
import logging
import uuid
import json
import time
import hashlib

logger = logging.getLogger('django')

@login_required(login_url="/login/")
def view(request):
    user = {
        'name':request.user,
        'date':time.time()
    }

    _system_settings = system_settings.objects.all()
    _sync_status = sync_status.objects.all()
    if len(_system_settings) == 0:
        system_settings.objects.create(config_sync_type=0)
    
    return render_to_response('settings/view.html',{ 'user': user, 'settings': _system_settings[0], 'sync_status': _sync_status })

def get_ip(meta):
    x_forwarded_for = meta.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = meta.get('REMOTE_ADDR')
    return ip

def sync_config(request, action):
    if request.GET.get('access_key','') == system_settings.objects.last().config_sync_access_key:
        node_ip = get_ip(request.META)
        sync_task = sync_status.objects.filter(address=node_ip)
        s_config = system_settings.objects.all()[0]
        if action == "get_config":
            config = get_config(int(request.GET.get('scope', '1')))
            if config:
                if len(sync_task):
                    sync_task.delete()

                sync_status.objects.create(
                    address=node_ip,
                    update_time=datetime.now(),
                    status=1
                )
                content = { "flag":"Success", "context": config }
            else:
                content = { "flag":"Error", "context": "get config error" }
        elif action == "ack":
            if len(sync_task):
                task = sync_task[0]
                task.update_time = datetime.now()
                task.status = 2
                task.save()
                content = { "flag":"Success" }
            else:
                content = { "flag":"Error", "context": "task not found" }

        s_config.save()
        return HttpResponse(json.dumps(content), content_type="application/json,charset=utf-8")
    else:
        return HttpResponse(json.dumps({ "flag":"Error", "context": "access deny" }), content_type="application/json,charset=utf-8", status=401)

def save_sync(config):
    try:
        s_config = system_settings.objects.all()[0]
        if int(config.get('config_sync_type')) == 0:
            s_config.config_sync_type = 0
            s_config.config_sync_access_key = None
            s_config.config_sync_master_url = None
            s_config.config_sync_scope = None
            sync_status.objects.all().delete()
        elif int(config.get('config_sync_type')) == 1:
            s_config.config_sync_type = 1
            s_config.config_sync_access_key = str(uuid.uuid4())
            s_config.config_sync_master_url = None
            s_config.config_sync_scope = None
        elif int(config.get('config_sync_type')) == 2:
            if config.get('config_sync_master_api'):
                s_config.config_sync_type = 2
                s_config.config_sync_access_key = None
                s_config.config_sync_master_url = config.get('config_sync_master_api').strip('/')
                s_config.config_sync_scope = bool(config.get('config_sync_scope',''))
            else:
                return False
        else:
            return False

        s_config.save()
        return True
    except Exception, e:
        logger.error(str(e))
        return False

@is_auth
def admin_password(request, action):
    if action == "reset":
        try:
            User.objects.all().delete()
            content = { "flag":"Success" }
        except Exception, e:
            content = { "flag":"Error","context":str(e) }

    elif action == "modify":
        try:
            post = json.loads(request.body)
            old_pass = post['old_password']
            new_pass = post['new_password']
            verify_pass = post['verify_password']
            if old_pass and new_pass and verify_pass:
                user = User.objects.get(username=request.user)
                if user.check_password(old_pass) and new_pass == verify_pass:
                    user.set_password(verify_pass)
                    user.save()
                    content = { "flag":"Success" }
                else:
                    content = { "flag":"Error","context":"VerifyFaild" }

        except Exception, e:
            content = { "flag":"Error","context":str(e) }

    return HttpResponse(json.dumps(content))

def get_config(scope=0):
    try:
        upstream_config_qc = upstream_config.objects.all()
        proxy_config_qc = proxy_config.objects.all()
        u_config = serializers.serialize('json', upstream_config_qc)
        p_config = serializers.serialize('json', proxy_config_qc)

        config = {
            "main_config" : {"sha1":"","config":""},
            "system_config" : {"sha1":"","config":""},
            "upstream_config" : {"sha1":hashlib.sha1(u_config).hexdigest(),"config":u_config},
            "proxy_config" : {"sha1":hashlib.sha1(p_config).hexdigest(),"config":p_config},
        }
        # scope: [0, 1, 2]
        # 0 - proxy/upstream config
        # 1 - main/proxy/upstream config
        # 2 - system/main/proxy/upstream config

        if scope:
            main_config_qc = main_config.objects.all()
            system_config_qc = system_settings.objects.all()
            m_config = serializers.serialize('json', main_config_qc)
            s_config = serializers.serialize('json', system_config_qc)

            if scope == 1:
                config['main_config'] = {"sha1":hashlib.sha1(m_config).hexdigest(),"config":m_config}

            elif scope == 2:
                config['main_config'] = {"sha1":hashlib.sha1(m_config).hexdigest(),"config":m_config}
                config['system_config'] = {"sha1":hashlib.sha1(s_config).hexdigest(),"config":s_config}
            
        return config
        
    except Exception, e:
        return None
    
def import_config(config):
    try:
        main_config_qc = main_config.objects.all()
        system_config_qc = system_settings.objects.all()
        proxy_config_qc = proxy_config.objects.all()
        upstream_config_qc = upstream_config.objects.all()

        m_config = config['main_config']
        s_config = config['system_config']
        p_config = config['proxy_config']
        u_config = config['upstream_config']
        
        if m_config.get('config', False):
            if hashlib.sha1(m_config.get('config')).hexdigest() == m_config.get('sha1'):
                main_config_qc.delete()
                for obj in serializers.deserialize("json", m_config.get('config')):
                    obj.save()
            else:
                return False

        if s_config.get('config', False):
            if hashlib.sha1(s_config.get('config')).hexdigest() == s_config.get('sha1'):
                system_config_qc.delete()
                for obj in serializers.deserialize("json", s_config.get('config')):
                    obj.save()
            else:
                return False

        if p_config.get('config', False) and u_config.get('config', False):
            if hashlib.sha1(p_config.get('config')).hexdigest() == p_config.get('sha1') and hashlib.sha1(u_config.get('config')).hexdigest() == u_config.get('sha1'):
                upstream_config_qc.delete()
                for obj in serializers.deserialize("json", u_config.get('config')):
                    obj.save()

                proxy_config_qc.delete()
                for obj in serializers.deserialize("json", p_config.get('config')):
                    obj.save()
            else:
                return False

        return True
    except Exception, e:
        logger.error(str(e))
        return False

@is_auth
def config(request, action):
    if action == "export":
        try:
            config = get_config(2)
            if config:
                content = { "flag":"Success", "context": config }
            else:
                content = { "flag":"Error", "context": "get config error" }
        except Exception,e:
            content = { "flag": "Error", "context": str(e) }

    elif action == "import":
        try:
            post = json.loads(request.body)
            if import_config(post):
                reload_config()
                content = { "flag":"Success" }
            else:
                content = { "flag":"Error", "context": "config import error" }
        except Exception,e:
            content = { "flag": "Error", "context": str(e) }
    elif action == "sync_update_token":
        try:
            save_sync({'config_sync_type':1})
            content = { "flag": "Success" }

        except Exception, e:
            content = { "flag": "Error", "context": str(e) }
    elif action == "sync_save_config":
        try:
            post = json.loads(request.body)
            if save_sync(post):
                content = { "flag": "Success" }
            else:
                content = { "flag": "Error", "context": "input error" }

        except Exception, e:
            content = { "flag": "Error", "context": str(e) }

    return HttpResponse(json.dumps(content))
