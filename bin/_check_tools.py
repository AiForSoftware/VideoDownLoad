import os, shutil, sys
os.environ['PATH'] = r'D:\CodeBuddy\VideoDownLoad\bin' + os.pathsep + os.environ.get('PATH', '')
print('PATH first dir:', os.environ['PATH'].split(os.pathsep)[0])
print('N_m3u8DL-RE:', shutil.which('N_m3u8DL-RE'))
print('aria2c:', shutil.which('aria2c'))
