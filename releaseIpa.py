# -*- coding: utf-8 -*-

from subprocess import Popen
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
import webbrowser

import httplib2
from apiclient.discovery import build
from apiclient.http import MediaFileUpload
from apiclient.http import BatchHttpRequest
from oauth2client.client import OAuth2WebServerFlow
from oauth2client.file import Storage

CREDENTIALS_FILE = 'credentials'
OAUTH_SCOPE = 'https://www.googleapis.com/auth/drive'
REDIRECT_URI = 'urn:ietf:wg:oauth:2.0:oob'

buildConfig = None

class BaseEditor(object):
    def __init__(self, filePath):
        self.fileHandle = codecs.open(filePath, 'r+', 'utf-8')
        self.fileData = self.fileHandle.read()
            
    def commit(self):
        self.fileHandle.seek(0)
        self.fileHandle.truncate()
        self.fileHandle.write(self.fileData)
        self.fileHandle.close()
        
    def discard(self):
        self.fileHandle.close()

class PlistEditor(BaseEditor):
    def __init__(self, filePath):
        super(PlistEditor, self).__init__(filePath)
    
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
    
def generateUniqueFileName(fileName):
    nameTuple = os.path.splitext(fileName)
    number = 1
    while os.path.exists(fileName):
        number += 1
        fileName = '%s_%d%s' % (nameTuple[0], number, nameTuple[1])
    return fileName
    
def issueCommand(command):
    print 'issue', command
    if isinstance(command, unicode):
        command = command.encode('utf-8')
    arguments = shlex.split(command)
    
    logFile = generateUniqueFileName('issueCommandLog')
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
    
def printProgress(progress, ongoing):
    message = '%3d%%' % min(progress, 100)
    if ongoing:
        sys.stdout.write(message)
        sys.stdout.write('\b' * len(message))
        sys.stdout.flush()
    else:
        print message
        
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
    
class GoogleDriveManager(object):
    def __init__(self):
        self.service = None
        self.http = httplib2.Http()
        
    def authorize(self, credentials):
        self.http = credentials.authorize(self.http)
        self.service = build('drive', 'v2', http=self.http)
        
    def makeDirectory(self, directory):
        components = splitPathIntoComponents(directory)
        folderID = None
        folderCreated = False
        for component in components:
            if not folderCreated:
                if folderID:
                    queriedFolder = self.service.children().list(folderId = folderID, q = 'mimeType=\'application/vnd.google-apps.folder\' and title=\'%s\'' % component).execute()
                else:
                    queriedFolder = self.service.files().list(q = 'mimeType=\'application/vnd.google-apps.folder\' and title=\'%s\'' % component).execute()
            if folderCreated or len(queriedFolder['items']) < 1:
                body = {
                    'title': component,
                    'mimeType': 'application/vnd.google-apps.folder'
                }            
                if folderID:
                    body['parents'] = [{
                        'id': folderID
                    }]
                folderID = self.service.files().insert(body = body).execute()['id']
                folderCreated = True
            else:
                folderID = queriedFolder['items'][0]['id']
        return folderID
        
    def insertFile(self, filePath, folderID, progressCallback = None):
        media_body = MediaFileUpload(filePath, mimetype='application/octet-stream', resumable=True)
        body = {
            'title': os.path.split(filePath)[1],
            'mimeType': 'application/octet-stream',
            'parents': [{
  	            'kind': 'drive#fileLink',
  	            'id': folderID
            }]
        }
        
        uploadRequest = self.service.files().insert(body = body, media_body = media_body)
        uploadedFile = None
        if callable(progressCallback):
            while uploadedFile is None:
                uploadStatus, uploadedFile = uploadRequest.next_chunk()
                if uploadStatus:
                    progressCallback(uploadStatus.progress())
                elif uploadedFile:
                    progressCallback(1)
        else:
            uploadedFile = uploadRequest.execute()
                
        return uploadedFile['id']
        
    def insertPermission(self, fileIDs, permission):
        makeRequest = lambda i: self.service.permissions().insert(fileId = fileIDs[i], body = permission)
        return GoogleDriveManager.executeMultipleRequests(fileIDs, makeRequest)
        
    def getFileInfo(self, fileIDs):
        makeRequest = lambda i: self.service.files().get(fileId = fileIDs[i])
        return GoogleDriveManager.executeMultipleRequests(fileIDs, makeRequest)
        
    @staticmethod
    def executeMultipleRequests(responseOrder, makeRequest):
        responses = [None for i in xrange(len(responseOrder))]
        def batchCallback(request_id, response, exception):
            if exception:
                return
            responses[responseOrder.index(request_id)] = response

        batch = BatchHttpRequest()
        for i in xrange(len(responseOrder)):
            batch.add(makeRequest(i), request_id = responseOrder[i], callback = batchCallback)
        batch.execute()
        return responses
    
def uploadToGoogleDrive(filePaths, transferInfo):
    if not filePaths:
        return []
        
    credentialsStorage = Storage(CREDENTIALS_FILE)
    credentials = credentialsStorage.get()
    if not credentials or not credentials.refresh_token:
        flow = OAuth2WebServerFlow(transferInfo['CLIENT_ID'], transferInfo['CLIENT_SECRET'], OAUTH_SCOPE, REDIRECT_URI)
        authorize_url = flow.step1_get_authorize_url()
        webbrowser.open_new(authorize_url)
        print 'Could not find valid credentials. Re-request access rights.'
        code = raw_input('Enter verification code: ').strip()
        credentials = flow.step2_exchange(code)
        credentialsStorage.put(credentials)
    
    driveManager = GoogleDriveManager()
    driveManager.authorize(credentials)

    fileIDs = []
    targetFolderID = driveManager.makeDirectory(transferInfo['GOOGLE_DRIVE_PATH'])
    for filePath in filePaths:
        print 'uploading %s......' % filePath,
        uploadedFileID = driveManager.insertFile(filePath, targetFolderID, lambda progress: printProgress(progress * 100, True))
        printProgress(100, False)
        fileIDs.append(uploadedFileID)
        
    new_permission = {
        'type': 'anyone',
        'role': 'reader',
        'withLink': True
    }
    driveManager.insertPermission(fileIDs, new_permission)

    # get the link
    uploadedFileInfo = driveManager.getFileInfo(fileIDs)
    return map(lambda fileInfo: fileInfo['webContentLink'], uploadedFileInfo)
    
class FTPUploadProgressHandler(object):
    def __init__(self, expectedSize):
        self.__totalUploadedSize = 0
        self.expectedSize = expectedSize
        
    def update(self, uploadedSize):
        self.__totalUploadedSize += uploadedSize
        printProgress(int(self.__totalUploadedSize / float(self.expectedSize) * 100), self.__totalUploadedSize < self.expectedSize)
    
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
            print 'uploading %s......' % filePath,
            fileHandles.append(open(filePath, 'rb'))
            FTPCommand = 'STOR %s' % os.path.split(filePath)[1]
            blockSize = 8192
            progressHandler = FTPUploadProgressHandler(os.path.getsize(filePath))
            FTPClient.storbinary(FTPCommand, fileHandles[-1], blockSize, lambda block: progressHandler.update(blockSize))
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
    return '<ul>%s</ul>' % HTMLListItems
    
def indexOfValidValue(iterable):
    for index, value in enumerate(iterable):
        if value:
            return index
    return -1
    
class MailBodyEditor(BaseEditor):
    def __init__(self, filePath):
        super(MailBodyEditor, self).__init__(filePath)    
    
    def linkifyBugCodes(self, bugURLMap):
        if not bugURLMap:
            return
            
        bugURLs = bugURLMap.items()
        # a '#' followed by a pattern represents an issue
        bugURLPattern = '|'.join(['#(%s)' % bugURL[0] for bugURL in bugURLs])
    
        def getBugURL(match):
            groupIndex = indexOfValidValue(match.groups(None)) + 1
            URL = bugURLs[groupIndex - 1][1].format(**{'BUG_CODE': match.group(groupIndex)})
            return '<a href="%s">%s</a>' % (URL, match.group(groupIndex))
        self.fileData = re.sub(bugURLPattern, getBugURL, self.fileData)
    
    def replaceKeywords(self, keywordDict):
        self.fileData = self.fileData.format(**keywordDict)

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
    if not issueCommand('svn update'):
        return
    
    # version should be fixed
    plistEditor = PlistEditor(buildConfig['INFO_PLIST_PATH'])
    plistEditor.replaceSimpleValue('CFBundleShortVersionString', buildConfig['APP_VERSION'])
    plistEditor.commit()
    
    # generate ipas
    ipasToExport = buildConfig['BUILD_PROFILES']
    ipas = map(exportIpa, ipasToExport)
    if not ipas or not all(ipas):
        return
    
    # commit the info plist
    logMessage = buildConfig['COMMIT_LOG_TEMPLATE'].format(**buildConfig)
    commitOptions = []
    commitOptions.append(optionGenerator('-m', logMessage))
    if 'SVN_USER' in buildConfig and 'SVN_PASSWORD' in buildConfig:
        commitOptions.append(optionGenerator('--username', buildConfig['SVN_USER']))
        commitOptions.append(optionGenerator('--password', buildConfig['SVN_PASSWORD']))
    commitCommand = 'svn commit %s "%s"' % (' '.join(commitOptions), buildConfig['INFO_PLIST_PATH'])
    if not issueCommand(commitCommand):
        return
    
    zippedIpas = zip(ipas, ipasToExport)
    
    os.chdir(thisFileFolderName)
    
    # upload to Google Drive
    ipasToUploadToGoogleDrive = filteredIpas(zippedIpas, lambda ipaTuple: ipaTuple[1]['uploadsToGoogleDrive'])
    print 'upload %s to %s of Google Drive' % (str(ipasToUploadToGoogleDrive), buildConfig['GOOGLE_API_CLIENT_INFO']['GOOGLE_DRIVE_PATH'])
    GDriveLinkList = uploadToGoogleDrive(ipasToUploadToGoogleDrive, buildConfig['GOOGLE_API_CLIENT_INFO'])
    if len(ipasToUploadToGoogleDrive) != len(GDriveLinkList) or not all(GDriveLinkList):
        return
        
    # upload to FTP server
    ipasToUploadToFTPServer = filteredIpas(zippedIpas, lambda ipaTuple: ipaTuple[1]['uploadsToFTPServer'])
    print 'upload %s to %s of %s' % (str(ipasToUploadToFTPServer), buildConfig['FTP_SERVER_BUILD_DIRECTORY'], buildConfig['FTP_SERVER_URL'])
    FTPLinkList = uploadToFTPServer(ipasToUploadToFTPServer, buildConfig)
    if len(ipasToUploadToFTPServer) != len(FTPLinkList):
        return
    
    # find description for the link
    zippedGDriveIpaList = zip(ipasToUploadToGoogleDrive, GDriveLinkList)
    zippedFTPIpaList = zip(ipasToUploadToFTPServer, FTPLinkList)
    ipaDict = dict(zippedIpas)
    linkDescriptions = [(ipaTuple[1], ipaDict[ipaTuple[0]]['versionDescription']) for ipaTuple in zippedGDriveIpaList + zippedFTPIpaList]
    
    # send the notification mail
    if GDriveLinkList or FTPLinkList:
        mailTransferInfo = buildConfig['MAIL_TRANSFER_INFO']
        print 'send notification mail to %s' % str(mailTransferInfo['toUsers'])
        mailTitle = mailTransferInfo['titleTemplate'].format(**buildConfig)
        bodyEditor = MailBodyEditor(mailTransferInfo['bodyFile'])
        keywordDict = buildConfig.copy()
        keywordDict['DOWNLOAD_LINKS'] = generateHTMLHyperlinkListItems(GDriveLinkList + FTPLinkList, dict(linkDescriptions))
        bodyEditor.replaceKeywords(keywordDict)
        bodyEditor.linkifyBugCodes(mailTransferInfo.get('bugCodeURLs', None))
        sendNotificationMail(mailTitle, bodyEditor.fileData, mailTransferInfo)
        bodyEditor.discard()

thisFileFolderName = os.path.split(os.getcwd())[1]
projectPath = os.path.split(os.getcwd())[0]
appName = os.path.split(projectPath)[1] # app name defaults to the folder name where app resides
appName = appName.replace(' ', '') # trim the spaces

if '__main__' == __name__:
    main()