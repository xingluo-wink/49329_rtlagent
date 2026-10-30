"""Honor image-provided EDA paths while keeping scratch work on local disk."""
import os
from pathlib import Path

def vivado():
    return Path(os.environ.get('XILINX_VIVADO','/tools/Xilinx/2026.1/Vivado'))

def install(common):
    original=common.env_for
    def env_for(model=None):
        env=original(model)
        for name in ('XILINX_ROOT','XILINX_VIVADO','XILINX_VITIS','XILINXD_LICENSE_FILE','FLEXLM_TIMEOUT'):
            if name in os.environ:env[name]=os.environ[name]
        env['XILINX_VIVADO']=str(vivado())
        env['PATH']=str(vivado()/'bin')+os.pathsep+env['PATH']
        for name,default in (('EDA_TMP','/var/tmp/eda'),('AGENT_TMP','/var/tmp/rtlagent-work'),('SELFTEST_TMP','/var/tmp/rtlagent-judge')):
            env[name]=os.environ.get(name,default);Path(env[name]).mkdir(parents=True,exist_ok=True)
        return env
    common.env_for=env_for
