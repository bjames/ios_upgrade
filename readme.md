**Usage**

1. Clone the repository: git clone --recursive github.com/bjames/ios_upgrade.git
2. Create a new virtual environment: virtualenv venv
3. Activate the virtual environment: source venv/bin/activate
4. Install required modules: python -m pip install -r requirements.txt
5. Modify ios_upgrade.yml as needed

**Configuration**

- threads: The number of threads to be spawned by the script. If set to 1, the script only upgrades one device at a time. This can be useful if a single device needs multiple updates (ie. Upgrading a 4510 from 3.6.1 to 3.8.6 using SSO can be done without a reload if you upgrade from 3.6.1 to 3.6.4 to 3.6.6 and finally to 3.8.6), or when upgrading redundant pairs of devices. 
- change_time: The time the update should take place. If this time has already passed, then the script waits until the same time on the next day. The script can be sent to the background and then disowned (if you would like close the SSH session) or left running in the foreground.
- default: These are the default device settings. Any settings defined here can be overridden in the target_device list
- target_devices: A list of hostnames or IP addresses. Default settings may be overridden as follows:
```
    - 192.168.1.1 # this device only uses default settings
    - 192.168.0.1 # this device overrides default settings
      image_name: c2960-lanlitek9-mz.150-2.SE11.bin
      image_md5: 885ed3dd7278baa11538a51827c2c9f8
```

**Submodules**

- smtp_relay: Contains functions related to sending emails, supports anonymous smtp relay
- ios_facts: Gathers information about IOS devices and stores them in a dictionary

