# fs.py: Convert a virtual appliance image in an EC2 AMI
#
# Copyright 2008, Red Hat  Inc.
# Joseph Boggs <jboggs@redhat.com>
# Copyright 2018, Neal Gompa <ngompa13@gmail.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Library General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.

from __future__ import division

from builtins import str
from builtins import object
from past.utils import old_div
import os
import subprocess
import logging
import sys
import shutil
import time
 
class LoopBackDiskImage(object): 

    def setup_fs(self,imagefile,tmpdir):
            loop_devices = [] 
            loop_partition_dict = {} 
            tmproot = tmpdir + "-tmproot"
            tmpimage = tmpdir + "-tmpimage"            
            
            logging.debug("TMPDIR: " + tmpdir)
            free_loop_dev = os.popen("/sbin/losetup -f")
            loop_device = free_loop_dev.read().strip()

            if not loop_device:
                sys.exit(1)
     
            os.system("/sbin/losetup %s %s" % (loop_device,imagefile))
            os.system("/sbin/kpartx -a %s" % loop_device)

            loop_partitions = os.popen("/sbin/kpartx -lv %s|awk {'print $1'}" % loop_device)

            for dev in loop_partitions:
                dev = dev.strip()
                label = os.popen("e2label /dev/mapper/%s 2>&1 " % dev)
                label = label.read().strip()
                if label.startswith("e2label"):
                    logging.error("Unable to detect partition label on %s, continuing anyways, if %s is a swap partition, no action is needed" % (dev,dev))          
                else:    
                    loop_partition_dict[dev] = label  
                    logging.debug( dev + " : " + label)

            dev = list(loop_partition_dict.values())
            dev.sort()
            os.mkdir(tmproot) 

            for value in dev:
                for key in list(loop_partition_dict.keys()):
                    if (value == loop_partition_dict[key]):
                        ld = os.popen("/sbin/losetup -f")
                        loop_partition_device = ld.read().strip()
                        if not loop_device:
                            logging.error("Please review your loopback device settings and remove unneeded ones")
                            sys.exit(1)
                        os.system("mount -o loop /dev/mapper/%s %s%s" % (key,tmproot,value))
            
            tmp_disk_space = os.popen("du -s %s|awk {'print $1'}" % tmproot)
            tmp_disk_space= old_div(int(tmp_disk_space.read()), 1024)
            logging.info("Disk Space Required: %sM" % str(tmp_disk_space))

            new_disk_space = int(tmp_disk_space + ((tmp_disk_space * 0.30) + 150))

            logging.info("\nCreating a new disk image with additional freespace: " + str(new_disk_space) + "M total")
            create_disk = os.system("dd if=/dev/zero of=%s/ec2-diskimage.img bs=1M count=%s" % (tmpimage,new_disk_space))
            os.system("mke2fs -Fj %s/ec2-diskimage.img" % tmpimage)
            if not loop_device:
                logging.error("Please review your loopback device settings and remove unneeded ones")
                sys.exit(1)
            os.system("mount -o loop %s/ec2-diskimage.img %s" % (tmpimage,tmpdir))
            
            logging.info("Performing rsync on all partitions to new root")
            os.system("rsync -u -r -a  %s/* %s" % (tmproot,tmpdir))

            dev.sort(reverse=True)

            for value in dev:
                logging.info("Unmounting %s%s" % (tmpdir,value))
                os.system("umount %s%s" % (tmproot,value))

            logging.info("Freeing loopdevices")
            os.system("kpartx -d %s" % loop_device)
            os.system("losetup -d %s" % loop_device)
            return
        
    def unmount(self,tmpdir):
        logging.debug("Unmounting directory %s" % tmpdir)
        os.system("/bin/umount %s" % tmpdir)
        return
    
    def cleanup(self,tmpdir):
        os.system("rm -rf %s/*" % tmpdir)
        return

class DirectoryImage(object): 

    def setup_fs(self,imagefile,tmpdir):
            tmproot = tmpdir + "-tmproot"
            tmpimage = tmpdir + "-tmpimage"            
            
            logging.info("TMPDIR: " + tmpdir)
            tmp_disk_space = os.popen("du -s %s|awk {'print $1'}" % imagefile)
            tmp_disk_space= old_div(int(tmp_disk_space.read()), 1024)
            logging.info("Disk Space Required: %sM" % str(tmp_disk_space))

            new_disk_space = int(tmp_disk_space + ((tmp_disk_space * 0.30) + 150))

            logging.info("Creating a new disk image with additional freespace: " + str(new_disk_space) + "M total")
            create_disk = os.system("dd if=/dev/zero of=%s/ec2-diskimage.img bs=1M count=%s" % (tmpimage,new_disk_space))
            os.system("mke2fs -Fj %s/ec2-diskimage.img" % tmpimage)

            free_loop_dev = os.popen("/sbin/losetup -f")
            loop_device = free_loop_dev.read().strip()

            if not loop_device:
                logging.error("Please review your loopback device settings and remove unneeded ones")
                sys.exit(1)

            os.system("mount -o loop %s/ec2-diskimage.img %s" % (tmpimage,tmpdir))
            
            logging.info("Performing rsync on all partitions to new root")
            os.system("rsync -u -r -a  %s/* %s" % (imagefile,tmpdir))
            return
        
    def unmount(self,tmpdir):
        logging.debug("Unmounting directory %s" % tmpdir)
        os.system("/bin/umount %s" % tmpdir)
        return
    
    def cleanup(self,tmpdir):
        os.system("rm -rf %s/*" % tmpdir)
        return

        
class LoopbackFSImage(object):
    
    def setup_fs(self,imagefile,tmpdir):
        logging.debug("Mounting %s to %s" % (imagefile,tmpdir))
        os.system("/bin/mount -o loop %s %s" % (imagefile,tmpdir))
        return
    
    def unmount(self,tmpdir):
        logging.debug("Unmounting directory %s" % tmpdir)
        os.system("/bin/umount %s" % tmpdir)
        return
    
    def cleanup(self,tmpdir):
        os.system("rm -rf %s/*" % tmpdir)
 
