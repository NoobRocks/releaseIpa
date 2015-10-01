releaseIpa
==========
[![Build Status](https://travis-ci.org/NoobRocks/releaseIpa.svg?branch=master)](https://travis-ci.org/NoobRocks/releaseIpa)

Done by releaseIpa.py, config.json and mailBody.html(configurable). 
However, editing release notes is still done manually.

### Requirements

* Python 2.6 or latest version of Python 2
* [Google APIs Client Library for Python](https://developers.google.com/api-client-library/python/)
* Command Line Tools for Xcode
* SVN command-line client

### config.json<a name="config.json"></a>

* BUILD_PROFILES is an array, each member specifying what configuration should be used to generate the ipa
* For GOOGLE_DRIVE_PATH and FTP_SERVER_BUILD_DIRECTORY, the script will create the intermediate folders if they do not exist
* versionDescription will be placed beside the link in the mail
* If provisioningProfile is empty, signingIdentity must be specified
* exportOptionsPlist is the plist used when the archive is exported. It should be put at the same directory as this script. Run `xcodebuild -help` for available keys in the plist
* If you wish to make a bug code clickable, specify the bug code pattern and corresponding URL in bugCodeURLs. See [config.json](https://github.com/NoobRocks/releaseIpa/blob/master/config.json#L36) for an example

### mailBody.html

* The body of this file will become the e-mail contents
* {FRIENDLY_APP_NAME}, {APP_VERSION}, and {DOWNLOAD_LINKS} will be replaced with the friendly app name, version and download links, respectively
* Prepend the bug code with a '#' to make it clickable. The '#' character will disappear in the resulting mail.(e.g. #NRS-123 becomes [NRS-123](http://jira_addr/browse/NRS-123))

### What releaseIpa Does

* edit plist
* SVN update and commit
* generate ipa
* upload to Google Drive and get the link for sharing
* upload to specified FTP server
* send the mail

### Usage

1. Create a folder, say buildScript, in the folder where the xcodeproj resides
1. Put releaseIpa.py, config.json and mailBody.html in it
1. Edit config.json(See [config.json](#config.json))
1. Edit mailBody.html
1. Launch the terminal and cd to the folder just created
1. Enter `python releaseIpa.py`

### TODO

* refresh the provisioning profiles in the script. [CUPERTINO](https://github.com/nomad/cupertino) may help.