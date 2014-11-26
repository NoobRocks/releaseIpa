# -*- coding: utf-8 -*-

from subprocess import Popen, PIPE
import os
from datetime import date
import shlex
import shutil
import re
import codecs
import traceback
import sys
import urlparse
import ftplib
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httplib2
from apiclient.discovery import build
from apiclient.http import MediaFileUpload
from oauth2client.client import OAuth2WebServerFlow
from oauth2client.file import Storage

CREDENTIALS_FILE = 'credentials'
OAUTH_SCOPE = 'https://www.googleapis.com/auth/drive'
REDIRECT_URI = 'urn:ietf:wg:oauth:2.0:oob'

buildConfig = None

class PlistEditor(object):
    def __init__(self, filePath):
        self.fileHandle = codecs.open(filePath, 'r+', 'utf-8')
        self.fileData = self.fileHandle.read()
    
    def replaceSimpleValue(self, key, value, valueType = 'string'):
        pattern = r'<key>%s</key>\s+<%s>.+</%s>' % (key, valueType, valueType)
        substitute = '<key>%s</key>\n\t<%s>%s</%s>' % (key, valueType, value, valueType)
        self.fileData = re.sub(pattern, substitute, self.fileData, flags = re.IGNORECASE)
        
    def readSimpleValue(self, key, valueType = 'string'):
        pattern = r'<key>%s</key>\s+<%s>(.+)</%s>' % (key, valueType, valueType)
        match = re.search(pattern, self.fileData, flags = re.IGNORECASE)
        if match is not None:
            return match.group(1)
        else:
            return None
            
    def commit(self):
        self.fileHandle.seek(0)
        self.fileHandle.truncate()
        self.fileHandle.write(self.fileData)
        self.fileHandle.close()
        
    def discard(self):
        self.fileHandle.close()

def generateBuildName(appName, appVersion, appBuild = None, suffix = None):
    def generateCurrentDateString():
        currentDate = date.today()
        return '%04d%02d%02d' % (currentDate.year, currentDate.month, currentDate.day)
    
    ipaNameComponents = [appName, generateCurrentDateString(), appVersion]
    isValidComponent = lambda c: bool(isinstance(c, basestring) and c)
    if isValidComponent(appBuild):
        ipaNameComponents.append(appBuild)
    if isValidComponent(suffix):
        ipaNameComponents.append(suffix)
    return '_'.join(ipaNameComponents)
    
def issueCommand(command):
    print 'issue', command
    if isinstance(command, unicode):
        command = command.encode('utf-8')
    arguments = shlex.split(command)
    
    logFile = 'issueCommandLog'
    pOut = open(logFile, 'w+')
    p = Popen(arguments, stdout = pOut)
    p.wait()
    pOut.close()
    if p.returncode != os.EX_OK:
        print 'returns %s. Refer to %s for details.' % (str(p.returncode), logFile)
    else:
        os.remove(logFile)
    return os.EX_OK == p.returncode
    
def incrementBuildNumber(appBuild):
    return str(int(appBuild) + 1)
    
optionGenerator = lambda name, value: '%s "%s"' % (name, value) if name and value else name or '"%s"' % value

def exportIpa(ipaInfo):
    # edit plist
    plistEditor = PlistEditor(buildConfig['INFO_PLIST_PATH'])
    plistEditor.replaceSimpleValue('CFBundleIdentifier', ipaInfo['bundleIdentifier'])
    appBuild = None
    canContinue = True
    if buildConfig['INCREMENT_BUILD_NUMBER']:
        appBuild = plistEditor.readSimpleValue('CFBundleVersion')
        try:
            appBuild = incrementBuildNumber(appBuild)
            plistEditor.replaceSimpleValue('CFBundleVersion', appBuild)
        except:
            canContinue = False
            excInfo = sys.exc_info()
            traceback.print_exception(excInfo[0], excInfo[1], excInfo[2], limit = 2, file = sys.stdout)
    plistEditor.commit()
    if not canContinue:
        return

    print 'Build %s(%s)' % (buildConfig['APP_VERSION'], appBuild)
    
    # clean
    cleanCommand = 'xcodebuild clean'
    issueCommand(cleanCommand)
    
    buildName = generateBuildName(appName, buildConfig['APP_VERSION'], appBuild, ipaInfo['ipaNameSuffix'])
    outputFolder = os.path.join(buildConfig['EXPORT_PATH_PREFIX'], appName)

    # archive
    scheme = ipaInfo['scheme']
    archivePath = os.path.join(outputFolder, 'archives', buildName + '.xcarchive')
    archiveCommand = 'xcodebuild -scheme "%s" archive -archivePath "%s"' % (scheme, archivePath)
    if os.path.exists(archivePath):
        shutil.rmtree(archivePath)
    if not issueCommand(archiveCommand):
        return

    # export
    exportPath = os.path.join(outputFolder, buildName + '.ipa')
    exportOptions = []
    exportOptions.append(optionGenerator('-exportArchive', ''))
    exportOptions.append(optionGenerator('-exportFormat', 'ipa'))
    exportOptions.append(optionGenerator('-archivePath', archivePath))
    exportOptions.append(optionGenerator('-exportPath', exportPath))
    exportProvisioningProfile = ipaInfo['provisioningProfile']
    if exportProvisioningProfile:
        exportOptions.append(optionGenerator('-exportProvisioningProfile', exportProvisioningProfile))
    else:
        exportOptions.append(optionGenerator('-exportSigningIdentity', ipaInfo['signingIdentity']))    
    exportCommand = 'xcodebuild %s' % ' '.join(exportOptions)
    if os.path.exists(exportPath):
        os.remove(exportPath)
    if issueCommand(exportCommand):
        print exportPath, 'generated'
    
    return exportPath
    
def GoogleDriveMakeWholeDirectory(driveService, directory):
    components = splitPathIntoComponents(directory)
    folderID = None
    folderCreated = False
    for component in components:
        if not folderCreated:
            if folderID:
                queriedFolder = driveService.children().list(folderId = folderID, q = 'mimeType=\'application/vnd.google-apps.folder\' and title=\'%s\'' % component).execute()
            else:
                queriedFolder = driveService.files().list(q = 'mimeType=\'application/vnd.google-apps.folder\' and title=\'%s\'' % component).execute()
        if folderCreated or len(queriedFolder['items']) < 1:
            if folderID:
                body = {
                    'title': component,
                    'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [{
                    	'id': folderID
                    }]
                }
            else:
                body = {
                    'title': component,
                    'mimeType': 'application/vnd.google-apps.folder'
                }
            folderID = driveService.files().insert(body = body).execute()['id']
            folderCreated = True
        else:
            folderID = queriedFolder['items'][0]['id']
    return folderID
    
def uploadToGoogleDrive(filePaths, transferInfo):
    if not filePaths:
        return []
        
    credentialsStorage = Storage(CREDENTIALS_FILE)
    credentials = credentialsStorage.get()
    if not credentials or not credentials.refresh_token:
        flow = OAuth2WebServerFlow(transferInfo['CLIENT_ID'], transferInfo['CLIENT_SECRET'], OAUTH_SCOPE, REDIRECT_URI)
        authorize_url = flow.step1_get_authorize_url()
        print 'Go to the following link in your browser: ' + authorize_url
        code = raw_input('Enter verification code: ').strip()
        credentials = flow.step2_exchange(code)
        credentialsStorage.put(credentials)
        
    http = httplib2.Http()
    http = credentials.authorize(http)
    
    drive_service = build('drive', 'v2', http=http)

    filesUploaded = []
    targetFolderID = GoogleDriveMakeWholeDirectory(drive_service, transferInfo['GOOGLE_DRIVE_PATH'])
    for filePath in filePaths:
        # upload the file
        media_body = MediaFileUpload(filePath, mimetype='application/octet-stream', resumable=True)
        body = {
            'title': os.path.split(filePath)[1],
            'mimeType': 'application/octet-stream',
            'parents': [{
  	            'kind': 'drive#fileLink',
  	            'id': targetFolderID
            }]
        }
        uploadedFile = drive_service.files().insert(body = body, media_body = media_body).execute()

        # modify the permission
        new_permission = {
            'type': 'anyone',
            'role': 'reader',
            'withLink': True
        }
        drive_service.permissions().insert(fileId = uploadedFile['id'], body = new_permission).execute()

        # get the link
        uploadedFile = drive_service.files().get(fileId = uploadedFile['id']).execute()
        filesUploaded.append(uploadedFile['webContentLink'])
    return filesUploaded
    
def splitPathIntoComponents(path):
    components = []
    if not isinstance(path, basestring):
        return components
    
    while True:
        pathTuple = os.path.split(path)
        components.insert(0, pathTuple[1])
        path = pathTuple[0]        
        if not path:
            break
            
    return components
    
def FTPMakeWholeDirectory(FTPClient, directory):
    components = splitPathIntoComponents(directory)
    for component in components:
        try:
            FTPClient.cwd(component)
        except ftplib.error_perm:
            FTPClient.mkd(component)
            FTPClient.cwd(component)
    
def uploadToFTPServer(filePaths, transferInfo):
    if not filePaths:
        return []
        
    loginInfo = urlparse.urlparse(transferInfo['FTP_SERVER_URL'])
    FTPClient = None
    fileHandles = []
    filesUploaded = []
    try:
        FTPClient = ftplib.FTP(loginInfo.hostname, loginInfo.username, loginInfo.password)
        buildDir = transferInfo['FTP_SERVER_BUILD_DIRECTORY']
        # create directory if it does not exist
        try:
            FTPClient.cwd(buildDir)
        except ftplib.error_perm:
            print '%s may not exist. Create one' % buildDir
            FTPMakeWholeDirectory(FTPClient, buildDir)
        for filePath in filePaths:
            FTPCommand = 'STOR %s' % os.path.split(filePath)[1]
            fileHandles.append(open(filePath, 'rb'))
            FTPClient.storbinary(FTPCommand, fileHandles[-1])
            filesUploaded.append('%s://%s/%s/%s/%s' % (loginInfo.scheme, loginInfo.hostname, loginInfo.username, buildDir,\
                                 os.path.split(filePath)[1]))
    except:
        excInfo = sys.exc_info()
        traceback.print_exception(excInfo[0], excInfo[1], excInfo[2], limit = 2, file = sys.stdout)
    # cleanup
    if fileHandles:
        for fileHandle in fileHandles:
            fileHandle.close()
    try:
        if FTPClient:
            FTPClient.quit()
    except:
        pass
        
    return filesUploaded

def sendNotificationMail(title, body, transferInfo):
    container = MIMEMultipart()
    container['Subject'] = title
    container['From'] = transferInfo['SMTPUserAddress']
    container['To'] = ','.join(transferInfo['toUsers'])
    container.attach(MIMEText(body, 'html', 'utf-8'))
    
    SMTPClient = None
    try:
        SMTPClient = smtplib.SMTP(transferInfo['SMTPServer'])
        if SMTPClient.has_extn('STARTTLS'):
            SMTPClient.starttls()
        SMTPClient.login(transferInfo['SMTPUser'], transferInfo['SMTPPassword'])
        SMTPClient.sendmail(transferInfo['SMTPUserAddress'], transferInfo['toUsers'], container.as_string())
    except:
        excInfo = sys.exc_info()
        traceback.print_exception(excInfo[0], excInfo[1], excInfo[2], limit = 2, file = sys.stdout)
    try:
        if SMTPClient:
            SMTPClient.quit()
    except:
        pass

def filteredIpas(zippedIpas, condition):
    return [ipaTuple[0] for ipaTuple in zippedIpas if condition(ipaTuple) and bool(ipaTuple[0])]

def generateHTMLHyperlinkListItems(linkList, linkDescriptions):
    HTMLListItems = ''
    for link in linkList:
        if linkDescriptions.get(link, ''):
            HTMLListItems = HTMLListItems + '<li><a href="%s">%s</a>(%s)</li>\n' % (link, link, linkDescriptions[link])
        else:
            HTMLListItems = HTMLListItems + '<li><a href="%s">%s</a></li>\n' % (link, link)
    return HTMLListItems

def main():
    # check and load config
    canContinue = True
    global buildConfig
    configFile = None
    try:
        configFile = open('config.json', 'r')
        buildConfig = json.load(configFile)
        
        loadedKeysSet = set(buildConfig.keys())
        requiredKeysSet = set(['APP_VERSION', 'EXPORT_PATH_PREFIX', 'INFO_PLIST_PATH', 'FTP_SERVER_URL',\
                               'FTP_SERVER_BUILD_DIRECTORY', 'INCREMENT_BUILD_NUMBER', 'BUILD_PROFILES',\
                               'COMMIT_LOG_TEMPLATE', 'MAIL_TRANSFER_INFO', 'GOOGLE_API_CLIENT_INFO',\
                               'FRIENDLY_APP_NAME'])
        canContinue = loadedKeysSet.issuperset(requiredKeysSet)
        if not canContinue:
            print 'some required keys are missing'
    except:
        canContinue = False
        excInfo = sys.exc_info()
        traceback.print_exception(excInfo[0], excInfo[1], excInfo[2], limit = 2, file = sys.stdout)
    if configFile:
        configFile.close()
    if not canContinue:
        return

    print 'Export ipa of', appName
    
    os.chdir('..')
    
    # svn update
    issueCommand('svn update')
    
    # version should be fixed
    plistEditor = PlistEditor(buildConfig['INFO_PLIST_PATH'])
    plistEditor.replaceSimpleValue('CFBundleShortVersionString', buildConfig['APP_VERSION'])
    plistEditor.commit()
    
    # generate ipas
    ipasToExport = buildConfig['BUILD_PROFILES']
    ipas = map(lambda ipaInfo: exportIpa(ipaInfo), ipasToExport)
    if not ipas:
        return
    
    # commit the info plist
    logMessage = buildConfig['COMMIT_LOG_TEMPLATE'] % buildConfig['APP_VERSION']
    commitOptions = []
    commitOptions.append(optionGenerator('-m', logMessage))
    if 'SVN_USER' in buildConfig and 'SVN_PASSWORD' in buildConfig:
        commitOptions.append(optionGenerator('--username', buildConfig['SVN_USER']))
        commitOptions.append(optionGenerator('--password', buildConfig['SVN_PASSWORD']))
    commitCommand = 'svn commit %s "%s"' % (' '.join(commitOptions), buildConfig['INFO_PLIST_PATH'])
    issueCommand(commitCommand)
    
    zippedIpas = zip(ipas, ipasToExport)
    
    os.chdir(thisFileFolderName)
    
    # upload to Google Drive
    ipasToUploadToGoogleDrive = filteredIpas(zippedIpas, lambda ipaTuple: ipaTuple[1]['uploadsToGoogleDrive'])
    print 'upload %s to %s of Google Drive' % (str(ipasToUploadToGoogleDrive), buildConfig['GOOGLE_API_CLIENT_INFO']['GOOGLE_DRIVE_PATH'])
    GDriveLinkList = uploadToGoogleDrive(ipasToUploadToGoogleDrive, buildConfig['GOOGLE_API_CLIENT_INFO'])
        
    # upload to FTP server
    ipasToUploadToFTPServer = filteredIpas(zippedIpas, lambda ipaTuple: ipaTuple[1]['uploadsToFTPServer'])
    print 'upload %s to %s of %s' % (str(ipasToUploadToFTPServer), buildConfig['FTP_SERVER_BUILD_DIRECTORY'], buildConfig['FTP_SERVER_URL'])
    FTPLinkList = uploadToFTPServer(ipasToUploadToFTPServer, buildConfig)
    
    # find description for the link
    zippedGDriveIpaList = zip(ipasToUploadToGoogleDrive, GDriveLinkList)
    zippedFTPIpaList = zip(ipasToUploadToFTPServer, FTPLinkList)
    ipaDict = dict(zippedIpas)
    linkDescriptions = [(ipaTuple[1], ipaDict[ipaTuple[0]]['versionDescription']) for ipaTuple in zippedGDriveIpaList + zippedFTPIpaList]
    
    # send the notification mail
    if GDriveLinkList or FTPLinkList:
        mailTransferInfo = buildConfig['MAIL_TRANSFER_INFO']
        print 'send notification mail to %s' % str(mailTransferInfo['toUsers'])
        mailTitle = mailTransferInfo['titleTemplate'] % (buildConfig['FRIENDLY_APP_NAME'], buildConfig['APP_VERSION'])
        bodyFile = codecs.open(mailTransferInfo['bodyFile'], 'rb', 'utf-8')
        body = bodyFile.read()
        HTMLLinkListItems = generateHTMLHyperlinkListItems(GDriveLinkList + FTPLinkList, dict(linkDescriptions))
        body = body % (buildConfig['FRIENDLY_APP_NAME'], buildConfig['APP_VERSION'], HTMLLinkListItems)
        sendNotificationMail(mailTitle, body, mailTransferInfo)
        bodyFile.close()

thisFileFolderName = os.path.split(os.getcwd())[1]
projectPath = os.path.split(os.getcwd())[0]
appName = os.path.split(projectPath)[1] # app name defaults to the folder name where app resides
appName = appName.replace(' ', '') # trim the spaces

if '__main__' == __name__:
    main()