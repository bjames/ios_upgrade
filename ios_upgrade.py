import getpass, re, time, datetime, yaml, socket, sys

from netmiko import ConnectHandler, NetMikoAuthenticationException, NetMikoTimeoutException
from multiprocessing import Pool
from contextlib import contextmanager
from functools import partial
from difflib import HtmlDiff

# internally developed submodules
from smtp_relay.smtp_relay import send_email
from ios_facts.ios_facts import get_facts, get_redundancy_status 

'''
TODO: 
Make the interface look better
Testing
'''

@contextmanager
def poolcontext(*args, **kwargs):

    ''' provides an easy way to start/end processes '''

    pool = Pool(*args, **kwargs)
    yield pool
    pool.terminate()


def setup_change_time(script_settings):

    ''' converts the change time to a datetime object, uses the current time if no change time was provided '''

    if(script_settings['change_time']):

        # convert change_time to an int
        change_time = map(int, script_settings['change_time'].split(':'))

        # convert change_time to a datetime object, unpacking the list from earlier
        change_time = datetime.datetime.combine(datetime.datetime.today(), datetime.time(*change_time))

        # if the time has already passed, we want to run the update the next time the clock hits that time
        # prevents the script from running early
        if change_time < datetime.datetime.now():

            try:
                
                change_time = change_time.replace(day = change_time.day + 1)

            except ValueError as e:

                # handle out of range errors
                if 'day is out of range for month' in str(e):

                    if change_time.month != 12:

                        change_time = change_time.replace(day = 1)
                        change_time = change_time.replace(month = change_time.month + 1)

                    else:

                        change_time = change_time.replace(day = 1)
                        change_time = change_time.replace(month = 1)
                        change_time = change_time.replace(year = change_time.year + 1)

                else:

                    raise

                

    else:

        change_time = datetime.datetime.now()

    return change_time


def wait_for_change_window(change_time):

    ''' loops until we are within the change window, sleeping for 1 minute between iterations '''

    print('waiting until ' + change_time.strftime('%c'))

    while(change_time > datetime.datetime.now()):

        time.sleep(60)

    print('change beginning')


def software_install(ssh_session, boot_directory, image_name):

    ''' runs the software install wizard on 3850s '''

    ssh_session.send_command_timing('software install file ' + boot_directory + image_name)

    # prompt to proceed with reload
    ssh_session.send_command('yes', max_loops=30000, expect_string='[yes/no]:')

    try:

        # prompt to save config after reload (prompt may not appear if config hasn't been changed)
        ssh_session.send_command('yes', expect_string='[yes/no]:')

    except:

        pass


def wait_ssh_session_close(ssh_session):

    ''' Waits for the ssh session to end, useful after a reload is issued '''

    timer = 60

    while(ssh_session.is_alive() and timer > 0):

        timer -= 1
        time.sleep(1)

    if ssh_session.is_alive():
        raise IOError('SSH session stayed alive, reload may have failed for unknown reasons')


def wait_for_redundant_state(ssh_session, reload_max_time):

    ''' waits for device to return to standby hot following a stateful switchover '''
    
    standby_hot = False

    wait_ssh_session_close(ssh_session)

    timeout = time.time() + reload_max_time

    # typically the redundant SUP is immediately avaliable
    while(not ssh_session.is_alive() and time.time() < timeout):

        ssh_reconnect(ssh_session)

    while(not standby_hot and time.time() < timeout):

        try:

            standby_hot = get_redundancy_status(ssh_session)

        # When the redundant SUP is not Hot or Cold an attribute error is thrown, it can be ignored here
        except AttributeError:

            pass

    if not ssh_session.is_alive():

        raise IOError('SSH session was not restored following the stateful switchover')

    if not standby_hot:

        raise AttributeError('Redundant SUP did not return to standby hot following the stateful switchover')


def redundancy_reload_shelf(ssh_session):

    ''' performs a shelf reload on a switch with dual SUPs operating in RPR mode '''

    ssh_session.send_command('redundancy reload shelf', expect_string=']')

    ssh_session.send_command_timing('')


def redundancy_switchover(ssh_session):

    ''' performs an SSO switchover on 4500 series with redundant SUPs '''

    ssh_session.send_command_timing('redundancy force-switchover')



def wait_for_reload(ssh_session, reload_max_time):

    ''' waits for a device to finish reloading and restarts the ssh session '''

    # if the ssh session is still active the reload may not have occured yet
    try:
        
        wait_ssh_session_close(ssh_session)

    except Exception:

        raise


    # add 90 to the timeout in case the timeout expires during an ssh connection attempt (90 is the default netmiko ssh timeout)
    timeout = time.time() + reload_max_time + 90

    while(not ssh_session.is_alive() and time.time() < timeout):

        try: 
            
            ssh_reconnect(ssh_session)

        # Some weirdness may occur with the ssh session as the switch is booting. Ignore and retry. 
        except:

            pass

    if(not ssh_session.is_alive()):

        raise IOError('Switch failed to reload within the configured max reload time')


def reload_device(ssh_session, reload_verify):

    ''' reloads the device, throws an error if verification fails '''

    if reload_verify:

        output = ssh_session.send_command_timing('reload /verify', expect_string=']')

        if('ERROR' in output):
            
            raise AttributeError('reload /verify failed due to ERROR conditions. Output:\n' + output)

    else:

        ssh_session.send_command('reload', expect_string=']')

    ssh_session.send_command_timing('')


def set_boot_statement(ssh_session, boot_directory, image_name):

    ''' sets the device boot statement '''

    # clear the current boot variables
    ssh_session.send_config_set('no boot system')

    # set the new boot variable
    ssh_session.send_config_set('boot system ' + boot_directory + image_name)

    # save the config
    ssh_session.send_command_timing('copy run start')
    ssh_session.send_command('', expect_string='[OK]')


def set_confreg(ssh_session):

    ''' sets the configuration register '''

    ssh_session.send_config_set('config 0x2102')


def code_exists(ssh_session, image_name):

    ''' checks to see if the image already exists in flash '''

    output = ssh_session.send_command('dir')

    if image_name not in output:

        return False

    return True


def copy_code(ssh_session, boot_directory, image_name, image_md5, remote_directory):

    ''' copies code based on settings in the config file '''

    if not code_exists(ssh_session, image_name):

        ssh_session.send_command('copy ' + remote_directory + image_name + ' ' + boot_directory, expect_string=']?')

        # The previous command seems to break netmiko's ability to automatically detect the expect_string
        output = ssh_session.send_command_expect(image_name, max_loops=30000, expect_string='#')

        if('Error' in output):
        
            raise IOError('Error in file transfer. Output:\n' + output)


    if(image_md5 is not None):

        output = ssh_session.send_command_expect('verify /md5 ' + boot_directory + image_name + ' ' + image_md5, max_loops=3000)

        if('Verified' not in output):
            
            raise IOError('Transferred image MD5 sum does not match expected MD5 sum')


def email_builder(text):

    ''' returns the email body with new text appended to the bottom and wrapped in <p> tags '''

    return '<p>' + text + '</p>'


def validate_facts(ssh_session, facts, upgrade_settings):

    ''' 
        used to verify whether or not an upgrade will be successful based on confreg and running image values
        doesn't return anything, but raises an exception if an error upon failure
    '''

    if facts['confreg'] not in upgrade_settings['confreg']:

        if(upgrade_settings['fix_confreg']):

            set_confreg('ssh_session')
            print_status(ssh_session.hostname + ': confreg updated')
        
        else:

            raise AttributeError('configured confreg ' + facts['confreg'] + ' not in acceptable confreg list')

    if facts['running_image'] == upgrade_settings['image_name']:

        raise AttributeError('device is already running ' + upgrade_settings['image_name'])


def validate_intent(upgrade_settings, change_time):

    ''' validates that the YAML file is configured correctly based on user response '''
    
    for device_settings in upgrade_settings:

        # verify the current mode of the script
        if device_settings['install']:

            print 'This will INSTALL ' + device_settings['image_name'] + ' on ' + device_settings['hostname']

        else: 

            print 'This will copy ' + device_settings['image_name'] + ' to ' + device_settings['hostname']

    print '\nReload(s) will occur after ' + change_time.strftime('%c')

    response = raw_input('Proceed? [y/n] ')

    response = response.strip().upper()

    if response != 'Y' and response != 'YES':

        print('Please modify ios_upgrade.yml with the intended settings, then run the script')
        
        return False

    return True


def get_validate_credentials(device):

    ''' 
        gets username and password, opens an ssh session to verify the credentials, then closes the ssh session
        returns username and password

        Doing this prevents multiple threads from locking out an account due to mistyped creds
    '''

    # attempts to get the username, prompts if needed
    username = getpass.getuser()

    # prompts user for password
    password = getpass.getpass()

    authenticated = False

    while not authenticated:

        try:
            
            test_ssh_session = ssh_connect(device, username, password)
            test_ssh_session.disconnect()

        except NetMikoAuthenticationException:

            print 'authentication failed on ' + device + ' (CTRL + C to quit)'

            username = raw_input('Username: ')
            password = getpass.getpass()

        except NetMikoTimeoutException:

            print 'SSH timed out on ' + device
            raise

        else:

            # if there is no exception set authenticated to true
            authenticated = True

    return username, password


def make_facts_table(pre_facts, post_facts):

    ''' builds an html table containing our facts '''

    facts_table = '<table border="1"><tr><td></td>'
    facts_table_pre = ''
    facts_table_post = ''

    # create table heading
    for key in pre_facts.keys():

        if 'running_config' not in key:

            facts_table += '<td>' + key + '</td>'

    # add facts to the table
    for key in pre_facts:

        if 'running_config' not in key:

            facts_table_pre += '<td>' + str(pre_facts[key]) + '</td>'
            
            # since keys may not match in both dicts in some edge cases we need to handle KeyErrors
            try:

                facts_table_post += '<td>' + str(post_facts[key]) + '</td>'
            
            except KeyError:

                facts_table_post += '<td>NONE</td>'
            

    facts_table += '<tr><td>pre-change</td>' + facts_table_pre + '</tr><tr><td>post-change</td>' + facts_table_post + '</tr></table>'

    return facts_table


def finalize_email(device, pre_facts, post_facts, email_body):

    facts_table = make_facts_table(pre_facts, post_facts)

    email_body = '<h2>' + device + '</h2>' + email_body + facts_table + '<h3>Config changes</h3>'

    
    try:
        email_body += HtmlDiff().make_file(pre_facts['running_config'].splitlines(), 
                                            post_facts['running_config'].splitlines(), 
                                            context=True)
    
    # if a keyerror is encountered, post_facts may not have been gathered
    except KeyError:

        return email_body

    return email_body


def ssh_connect(device, username, password):
    
    ''' returns a netmiko ssh session '''

    # populate device information
    device = {
        'device_type': 'cisco_ios',
        'ip': device,
        'username': username,
        'password': password,
    }

    # connect to the device
    ssh_session = ConnectHandler(**device)

    return ssh_session


def ssh_reconnect(ssh_session):

    ''' cleanly reconnects a netmiko session following a disconnect '''

    ssh_session.establish_connection()
    ssh_session.session_preparation()


def validate_facts_copy_code(device_settings, username, password):

    ''' copies code to a single device '''

    # open an ssh session
    ssh_session = ssh_connect(device_settings['hostname'], username, password)
    pre_facts = get_facts(ssh_session)

    email_body = email_builder('copy code ' + device_settings['hostname'])

    try:

        print_status(device_settings['hostname'] + ': validating device state')

        validate_facts(ssh_session, pre_facts, device_settings)

        print_status(device_settings['hostname'] + ': copying and verifying IOS image')

        copy_code(ssh_session,
                    pre_facts['boot_directory'],
                    device_settings['image_name'],
                    device_settings['image_md5'],
                    device_settings['remote_directory'])

        print_status(device_settings['hostname'] + ': IOS imaged in flash and validated')

        # if the device has dual SUPs we must also copy to the slave SUP
        if pre_facts['number_sups'] == 2:

            print_status(device_settings['hostname'] + ': copying and verifying IOS image on standby SUP')

            copy_code(ssh_session, 'slave' + pre_facts['boot_directory'], 
                        device_settings['image_name'], 
                        device_settings['image_md5'], 
                        device_settings['remote_directory'])

            print_status(device_settings['hostname'] + ': IOS image in slave flash and validated')

        email_body += email_builder('Success')

    except Exception as e:
        
        print_status(device_settings['hostname'] + ': ' + str(e))
        email_body += email_builder(str(e))

    finally:

        return email_body


def upgrade_code(device_settings, username, password):

    ''' performs a code upgrade on a single device '''

    # start the ssh session
    ssh_session = ssh_connect(device_settings['hostname'], username, password)
    pre_facts = get_facts(ssh_session)

    email_body = ''

    try:

        validate_facts(ssh_session, pre_facts, device_settings)
    
        # All the code below will cause an outage. Use caution to keep checks in place when restructuring
        if(device_settings['install']):

            # special handling for install mode on 3850s
            if pre_facts['install_mode']:
            
                print_status(device_settings['hostname'] + ': IOS is running in install mode, beginning install mode upgrade')

                software_install(ssh_session, pre_facts['boot_directory'], device_settings['image_name'])

                print_status(device_settings['hostname'] + ': install complete, reloading')

                wait_for_reload(ssh_session, device_settings['reload_max_time'])

                print_status(device_settings['hostname'] + ': reload complete')


            else:

                # setting the boot statement works the same regardless of the number of SUPs
                set_boot_statement(ssh_session, pre_facts['boot_directory'], device_settings['image_name'])

                print_status(device_settings['hostname'] + ': boot statement updated')


                # single SUP devices
                if pre_facts['number_sups'] < 2:

                    reload_device(ssh_session, device_settings['reload_verify'])

                    print_status(device_settings['hostname'] + ': reloading')

                    wait_for_reload(ssh_session, device_settings['reload_max_time'])

                    print_status(device_settings['hostname'] + ': reload complete')

                # devices with more than 2 SUPs are unhandled
                elif pre_facts['number_sups'] == 2:

                    # handle SSO upgrades
                    if pre_facts['sso'] and pre_facts['standby_hot']:

                        # 1st switchover
                        redundancy_switchover(ssh_session)
        
                        print_status(device_settings['hostname'] + ': waiting for SSO')

                        wait_for_redundant_state(ssh_session, device_settings['reload_max_time'])

                        print_status(device_settings['hostname'] + ': upgrade complete on primary SUP')


                        # 2nd switchover
                        redundancy_switchover(ssh_session)

                        print_status(device_settings['hostname'] + ': waiting for SSO on secondary SUP')

                        wait_for_redundant_state(ssh_session, device_settings['reload_max_time'])

                        print_status(device_settings['hostname'] + ': upgrade complete on both SUPs')


                    # RPR upgrades
                    elif device_settings('reload_shelf_rpr'):

                        redundancy_reload_shelf(ssh_session)

                        print_status(device_settings['hostname'] + ': waiting for shelf reload')

                        wait_for_reload(ssh_session, device_settings['reload_max_time'])

                        print_status(device_settings['hostname'] + ': reload complete')

                else:

                    email_body += email_builder('device has more than 2 SUPs, not currently supported')

 
    except Exception as e:

        print_status(device_settings['hostname'] + ': ' + str(e))
        email_body += email_builder(str(e))

    finally:

        try:
            
            print_status(device_settings['hostname'] + ': gathering post change facts')
            post_facts = get_facts(ssh_session)
            print_status(device_settings['hostname'] + ': complete')
        
        except Exception:

            post_facts = {'error':'post change facts could not be gathered'}
     
        email_body = finalize_email(device_settings['hostname'], pre_facts, post_facts, email_body)
        
        return email_body


def merge_settings(device, script_settings):

    ''' merges the default and device specific dictionaries '''

    upgrade_settings = script_settings['default'].copy()
    upgrade_settings.update(device)

    return upgrade_settings


def set_upgrade_settings(script_settings):

    upgrade_settings = []
    
    # parse ios_upgrade.yml and build a list containing devices and their specific upgrade settings
    for device in script_settings['target_devices']:

        device_settings = merge_settings(device, script_settings)
        upgrade_settings.append(device_settings)

    return upgrade_settings


def print_status(status):

    ''' 
    prints the current device status 
    ignores any errors when script is ran in the backgroup
    '''

    try:
        print(status)

    except:
        pass

def main():

    # pull data from config file
    script_settings = yaml.safe_load(open("ios_upgrade.yml"))

    start_time = time.time()

    # start building the email
    email_subject = "IOS Upgrade - " + time.asctime(time.localtime(start_time))
    email_body = ""

    change_time = setup_change_time(script_settings)

    upgrade_settings = set_upgrade_settings(script_settings)

    # verify that the YAML actually contains what we want to do
    if not validate_intent(upgrade_settings, change_time):

        exit()

    # attempt to get the username from environment variables, prompt if needed
    username, password = get_validate_credentials(upgrade_settings[0]['hostname'])

    # copy code to devices
    if script_settings['pre_copy']:

        print_status('Copying code prior to change window')

        with poolcontext(processes=script_settings['threads']) as pool:

            upgrade_html = pool.map(partial(validate_facts_copy_code,
                                            username=username,
                                            password=password),
                                upgrade_settings)

    else:

        wait_for_change_window(change_time)

        with poolcontext(processes=script_settings['threads']) as pool:

            upgrade_html = pool.map(partial(validate_facts_copy_code,
                                            username=username,
                                            password=password),
                                upgrade_settings)

    with poolcontext(processes=script_settings['threads']) as pool:

        upgrade_html = pool.map(partial(upgrade_code, 
                                        username=username, 
                                        password=password), 
                                upgrade_settings)

    for html in upgrade_html:

        email_body += html

    total_time = time.time() - start_time
    total_time = time.strftime('%H:%M:%S', time.gmtime(total_time))

    email_body += email_builder('Total time: ' + total_time)

     
    send_email(subject = email_subject, body = email_body, recepient=script_settings['email_recipient'])

main()
