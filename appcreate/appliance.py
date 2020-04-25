#
# appliance.py: ApplianceImageCreator class
#
# Copyright 2007-2008, Red Hat  Inc.
# Copyright 2008, Daniel P. Berrange
# Copyright 2018, Neal Gompa
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

from __future__ import print_function
from builtins import chr
from builtins import range
import os
import os.path
import glob
import shutil
import zipfile
import tarfile
import subprocess
import logging
import re

from imgcreate.errors import *
from imgcreate.fs import *
from imgcreate.creator import *
from appcreate.partitionedfs import *
import progress.bar as progress

class ApplianceImageCreator(ImageCreator):
    """Installs a system into a file containing a partitioned disk image.

    ApplianceImageCreator is an advanced ImageCreator subclass; a sparse file
    is formatted with a partition table, each partition loopback mounted
    and the system installed into an virtual disk. The disk image can
    subsequently be booted in a virtual machine or accessed with kpartx

    """

    def __init__(self, ks, name, disk_format, vmem, vcpu, releasever=None, no_compress=False):
        """Initialize a ApplianceImageCreator instance.

        This method takes the same arguments as ImageCreator.__init__()

        """
        ImageCreator.__init__(self, ks, name, releasever=releasever)

        self.__instloop = None
        self.__imgdir = None
        self.__disks = {}
        self.__disk_format = disk_format
        self.__compress = not no_compress

        #appliance parameters
        self.vmem = vmem
        self.vcpu = vcpu
        self.checksum = False
        self.appliance_version = None
        self.appliance_release = None

        #additional modules to include
        self.modules = ["sym53c8xx", "aic7xxx", "mptspi"]

        # This determines which partition layout we'll be using
        self.bootloader = None

    def _get_fstab(self):
        s = ""
        for mp in self.__instloop.mountOrder:
            p = None
            for p1 in self.__instloop.partitions:
                if p1['mountpoint'] == mp:
                    p = p1
                    break

            if not p['UUID'] is None:
                mountdev = p['UUID']
            else:
                mountdev = "LABEL=_%s" % p['mountpoint']
            s +=  "%(mountdev)s  %(mountpoint)s %(fstype)s    defaults,noatime 0 0\n" %  {
                'mountdev': mountdev,
                'mountpoint': p['mountpoint'],
                'fstype': p['fstype'] }
        return s

    def _create_mkinitrd_config(self):
        #write  to tell which modules to be included in initrd

        extramods = ""
        for module in self.modules:
            extramods += '%s ' % module

        mkinitrd = ""
        mkinitrd += "PROBE=\"no\"\n"
        mkinitrd += "MODULES=\"ext3 ata_piix sd_mod libata scsi_mod\"\n"
        mkinitrd += "MODULES=\"%s\"\n" % extramods
        mkinitrd += "rootfs=\"ext3\"\n"
        mkinitrd += "rootopts=\"defaults\"\n"

        logging.debug("Writing mkinitrd config %s/etc/sysconfig/mkinitrd" % self._instroot)
        os.makedirs(self._instroot + "/etc/sysconfig/", mode=644)
        cfg = open(self._instroot + "/etc/sysconfig/mkinitrd", "w")
        cfg.write(mkinitrd)
        cfg.close()

    #
    # Actual implementation
    #
    def _mount_instroot(self, base_on = None):
        self.__imgdir = self._mkdtemp()

        #list of partitions from kickstart file
        parts = kickstart.get_partitions(self.ks)
        # need to eliminate duplicate partitions
        # this is a bit of a hack but we assume the last partition for a given mount point is the one we want
        mountpoints = []
        toremove = []
        for part in parts:
            mp = part.mountpoint
            if mp in mountpoints:
                toremove.append(part)
            else:
                mountpoints.append(mp)
        for part in toremove:
            parts.remove(part)
        #list of disks where a disk is an dict with name: and size
        disks = []

        for i in range(len(parts)):
            if parts[i].disk:
                disk = parts[i].disk
            else:
                logging.debug("No --ondisk specified in partition line of ks file; assuming 'sda'")
                disk = "sda"

            size = parts[i].size * 1024 * 1024

            if len(disks) == 0:
                disks.append({ 'name': disk, 'size': size })
            else:
                found = 'false'
                for j in range(len(disks)):
                    if disks[j]['name'] == disk:
                        disks[j]['size'] = disks[j]['size'] + size
                        found = 'true'
                        break
                    else: found = 'false'
                if found == 'false':
                    disks.append({ 'name': disk, 'size': size })

        #create disk
        for item in disks:
            logging.debug("Adding disk %s as %s/%s-%s.raw" % (item['name'], self.__imgdir, self.name, item['name']))
            disk = SparseLoopbackDisk("%s/%s-%s.raw" % (self.__imgdir, self.name, item['name']), item['size'])
            self.__disks[item['name']] = disk


        # Search for bootloader package in package list
        packages = kickstart.get_packages(self.ks)
        # make this the default
        partition_layout = None
        # set bootloader only if it is enabled and use user specified partition_layout
        if ((not hasattr(self.ks.handler.bootloader, "disabled")) or
           (hasattr(self.ks.handler.bootloader, "disabled") and self.ks.handler.bootloader.disabled is False)):
            # check for extlinux in kickstart then grub2 and falling back to grub
            if hasattr(self.ks.handler.bootloader, "extlinux") and self.ks.handler.bootloader.extlinux is True:
                if 'syslinux-extlinux' in packages:
                    self.bootloader = 'extlinux'
                    partition_layout = 'msdos'
                elif 'extlinux-bootloader' in packages:
                    self.bootloader = 'extlinux-bootloader'
                    partition_layout = 'msdos'
                else:
                    logging.warning("WARNING! syslinux-extlinux package not found.")
            if self.bootloader is None:
                if 'grub2' in packages:
                    self.bootloader = 'grub2'
                elif 'grub' in packages:
                    self.bootloader = 'grub'
                else:
                    logging.warning("WARNING! grub package not found.")
        else:
            # user explicitly disabled bootloader (i.e. not part of disk image)
            pass

        # Determine partition type if it was not forced
        if partition_layout is None:
            if (hasattr(self.ks.handler.clearpart, "disklabel")
               and self.ks.handler.clearpart.disklabel is not None and self.ks.handler.clearpart.disklabel != ""):
                logging.debug("Using user set default disk label: {}".format(self.ks.handler.clearpart.disklabel))
                partition_layout = self.ks.handler.clearpart.disklabel
            else:
                partition_layout = 'msdos'
        else:
            logging.debug("Forcing disk label to %s, because bootloader is %s" % (partition_layout, self.bootloader))

        self.__instloop = PartitionedMount(self.__disks,
                                           self._instroot,
                                           partition_layout)

        for p in parts:
            if p.disk:
                self.__instloop.add_partition(int(p.size), p.disk, p.mountpoint, p.fstype)
            else:
                self.__instloop.add_partition(int(p.size), "sda", p.mountpoint, p.fstype)

        try:
            self.__instloop.mount()
        except MountError as e:
            raise CreatorError("Failed mount disks : %s" % e)

        self._create_mkinitrd_config()

    def _kernel_cmdline_append(self):
        options = self.ks.handler.bootloader.appendLine
        if options == '':
            options = 'rhgb quiet'
        lang = self.ks.handler.lang.lang
        if lang != '':
            options = '%s LANG=%s' % (options, lang)
        return options

    def _create_grub_devices(self, grubversion = 1):
        devs = []
        parts = kickstart.get_partitions(self.ks)
        for p in parts:
            dev = p.disk
            if dev != "" and not dev in devs:
                devs.append(dev)

        if devs == []:
            devs.append("sda")

        devs.sort()

        n = 0
        devmap = ""
        for dev in devs:
            devmap += "(hd%-d) /dev/%s\n" % (n, dev)
            n += 1

        if grubversion == 2:
            grubdir = "/boot/grub2"
        else:
            grubdir = "/boot/grub"

        logging.debug("Writing grub %s%s/device.map" % (self._instroot, grubdir))
        cfg = open(self._instroot + "%s/device.map" % grubdir, "w")
        cfg.write(devmap)
        cfg.close()

    def _get_grub_boot_config(self):
        bootdevnum = None
        rootdevnum = None
        rootdev = None
        for p in self.__instloop.partitions:
            if p['mountpoint'] == "/boot":
                bootdevnum = p['num'] - 1
            elif p['mountpoint'] == "/" and bootdevnum is None:
                bootdevnum = p['num'] - 1

            if p['mountpoint'] == "/":
                rootdevnum = p['num'] - 1
                if not p['UUID'] is None:
                    rootdev = p['UUID']
                else:
                    rootdev = "LABEL=_/"

        prefix = ""
        if bootdevnum == rootdevnum:
            prefix = "/boot"

        return (bootdevnum, rootdevnum, rootdev, prefix)

    def _create_grub_config(self):
        (bootdevnum, rootdevnum, rootdev, prefix) = self._get_grub_boot_config()
        options = self._kernel_cmdline_append()

        # NB we're assuming that grub config is on the first physical disk
        # ie /boot must be on sda, or if there's no /boot, then / must be sda

        # XXX don't hardcode default kernel - see livecd code
        grub = ""
        grub += "default=0\n"
        grub += "timeout=5\n"
        grub += "splashimage=(hd0,%d)%s/grub/splash.xpm.gz\n" % (bootdevnum, prefix)
        grub += "hiddenmenu\n"

        versions = []
        kernels = self._get_kernel_versions()
        for kernel in kernels:
            for version in kernels[kernel]:
                versions.append(version)

        if int(subprocess.Popen("ls " + self._instroot + "/boot/initramfs* | wc -l", shell=True, stdout=subprocess.PIPE).communicate()[0].decode("utf-8").strip()) > 0:
            initrd = "initramfs"
        else:
            initrd = "initrd"

        for v in versions:
            grub += "title %s (%s)\n" % (self.name, v)
            grub += "        root (hd0,%d)\n" % bootdevnum
            grub += "        kernel %s/vmlinuz-%s ro root=%s %s\n" % (prefix, v, rootdev, options)
            grub += "        initrd %s/%s-%s.img\n" % (prefix, initrd, v)

        logging.debug("Writing grub config %s/boot/grub/grub.conf" % self._instroot)
        if not os.path.isdir(self._instroot + "/boot/grub/"):
            os.mkdir(self._instroot + "/boot/grub/")
        cfg = open(self._instroot + "/boot/grub/grub.conf", "w")
        cfg.write(grub)
        cfg.close()

    def _get_extlinux_boot_config(self):
        bootdevnum = None
        rootdevnum = None
        rootdev = None
        for p in self.__instloop.partitions:
            if p['mountpoint'] == "/boot":
                bootdevnum = p['num'] - 1
            elif p['mountpoint'] == "/" and bootdevnum is None:
                bootdevnum = p['num'] - 1

            if p['mountpoint'] == "/":
                rootdevnum = p['num'] - 1
                if not p['UUID'] is None:
                    rootdev = p['UUID']
                else:
                    rootdev = "LABEL=_/"
        prefix = ""
        if bootdevnum == rootdevnum:
            prefix = "/boot"

        return (bootdevnum, rootdevnum, rootdev, prefix)

    def _create_extlinux_config(self):
        (bootdevnum, rootdevnum, rootdev, prefix) = self._get_grub_boot_config()
        options = self._kernel_cmdline_append()
        # Search for bootloader package in package list
        packages = kickstart.get_packages(self.ks)

        extlinux = "# extlinux.conf generated by appliance-creator\n"
        extlinux += "ui menu.c32\n"
        extlinux += "menu autoboot Welcome to %s. Automatic boot in # second{,s}. Press a key for options.\n" % (self.name)
        extlinux += "menu title %s Boot Options.\n" % (self.name)
        extlinux += "menu hidden\n"
        extlinux += "timeout 20\n"
        extlinux += "totaltimeout 600\n\n"

        versions = []
        kernels = self._get_kernel_versions()
        for kernel in kernels:
            for version in kernels[kernel]:
                versions.append(version)

        if int(subprocess.Popen("ls " + self._instroot + "/boot/initramfs* | wc -l", shell=True, stdout=subprocess.PIPE).communicate()[0].decode("utf-8").strip()) > 0:
            initrd = "initramfs"
        else:
            initrd = "initrd"

        for v in versions:
            extlinux += "label %s (%s)\n" % (self.name, v)
            extlinux += "\tkernel %s/vmlinuz-%s\n" % (prefix, v)
            extlinux += "\tappend ro root=%s %s\n" % (rootdev, options)
            if 'extlinux-bootloader' in packages:
                extlinux += "\tfdtdir %s/dtb-%s/\n" % (prefix, v)
            extlinux += "\tinitrd %s/%s-%s.img\n\n" % (prefix, initrd, v)


        logging.debug("Writing extlinux config %s/boot/extlinux/extlinux.conf" % self._instroot)
        cfg = open(self._instroot + "/boot/extlinux/extlinux.conf", "w")
        cfg.write(extlinux)
        cfg.close()

    def _copy_grub_files(self):
        imgpath = None
        # http://bugs.centos.org/view.php?id=4995
        # https://issues.jboss.org/browse/BGBUILD-267
        for machine in ["x86_64-redhat", "i386-redhat", "x86_64-unknown", "i386-unknown", "x86_64-pc", "i386-pc"]:
            imgpath = self._instroot + "/usr/share/grub/" + machine
            if os.path.exists(imgpath):
                break

        files = ["e2fs_stage1_5", "stage1", "stage2"]
        for f in files:
            path = imgpath + "/" + f
            if not os.path.isfile(path):
                raise CreatorError("grub not installed : "
                                   "%s not found" % path)

            logging.debug("Copying %s to %s/boot/grub/%s" %(path, self._instroot, f))
            shutil.copy(path, self._instroot + "/boot/grub/" + f)

    def _install_grub(self):
        (bootdevnum, rootdevnum, rootdev, prefix) = self._get_grub_boot_config()

        # Ensure all data is flushed to disk before doing grub install
        subprocess.call(["sync"])

        stage2 = "/boot/grub/stage2"
        setup = ""

        i = 0
        for name in list(self.__disks.keys()):
            loopdev = self.__disks[name].device
            setup += "device (hd%s) %s\n" % (i, loopdev)
            i = i + 1
        setup += "root (hd0,%d)\n" % bootdevnum
        setup += "setup --stage2=%s --prefix=%s/grub  (hd0)\n" % (stage2, prefix)
        setup += "quit\n"

        logging.debug("Installing grub to %s" % loopdev)

        subprocess.call(["mount", "--bind", "/dev", self._instroot + "/dev"])

        grub = subprocess.Popen(["chroot", self._instroot, "/sbin/grub", "--batch", "--no-floppy"],
                                stdin=subprocess.PIPE)

        grub.communicate(setup.encode("utf-8"))
        rc = grub.wait()

        subprocess.call(["umount", self._instroot + "/dev"])

        if rc != 0:
            raise MountError("Unable to install grub bootloader")

        logging.debug("Grub installed.")

    def _install_grub2(self):
        (bootdevnum, rootdevnum, rootdev, prefix) = self._get_grub_boot_config()

        i = 0
        for name in list(self.__disks.keys()):
            loopdev = self.__disks[name].device
            i = i + 1

        logging.debug("Installing grub2 to %s" % loopdev)

        # mount full /dev filesystem
        subprocess.call(["mount", "--bind", "/dev", self._instroot + "/dev"])

        rc = subprocess.call(["chroot", self._instroot, "grub2-install", "--no-floppy", "--grub-mkdevicemap=/boot/grub2/device.map", loopdev])

        if rc != 0:
            subprocess.call(["umount", self._instroot + "/dev"])
            raise MountError("Unable to install grub2 bootloader")

        mkconfig_dest = "/boot/grub2/grub.cfg"
        try:
            logging.debug(self._instroot + "/boot/grub2/grubenv")
            if os.path.islink(self._instroot + "/boot/grub2/grubenv"):
                dest=os.readlink(self._instroot + "/boot/grub2/grubenv")
                dest=dest.replace("grubenv","grub.cfg")
                os.symlink(dest,self._instroot+mkconfig_dest)
                logging.debug("Symlink created from %s to %s" % (dest,mkconfig_dest))
        except:
            logging.debug("Error creating symlink for %s" % mkconfig_dest)

        logging.debug("Grub2 installed.")
        logging.debug("Generating grub2 configuration file in (%s)..." % mkconfig_dest)

        # Generating grub2 config file
        subprocess.call(["chroot", self._instroot, "grub2-mkconfig", "-o", mkconfig_dest])

        # umount /dev filesystem
        subprocess.call(["umount", self._instroot + "/dev"])

        rootpartition = self.__instloop.partitions[rootdevnum]
        bootpartition = self.__instloop.partitions[bootdevnum]

        # Grub2 config file needs some cleanup because it has root device specified as a loop disk...
        grub2_cfg = open(self._instroot + "/boot/grub2/grub.cfg","r+")
        data = grub2_cfg.read()
        # Changing values for both - root and boot partitions
        if not bootpartition['UUID'] is None:
            data = re.sub(bootpartition['devicemapper'], bootpartition['UUID'], data)
        else:
            data = re.sub(bootpartition['devicemapper'], "LABEL=_%s" % bootpartition['mountpoint'], data)
        if not rootpartition['UUID'] is None:
            data = re.sub(rootpartition['devicemapper'], rootpartition['UUID'], data)
            data = re.sub(loopdev, rootpartition['UUID'], data)
        else:
            data = re.sub(rootpartition['devicemapper'], "LABEL=_%s" % rootpartition['mountpoint'], data)
            data = re.sub(loopdev, "LABEL=_%s" % rootpartition['mountpoint'], data)

        grub2_cfg.seek(0)
        grub2_cfg.truncate()
        grub2_cfg.write(data)
        grub2_cfg.close()

        logging.debug("Grub2 configuration file generated.")

    def _install_extlinux(self):
        i = 0
        for name in list(self.__disks.keys()):
            loopdev = self.__disks[name].device
            i = i + 1

        logging.debug("Installing extlinux bootloader to %s" % loopdev)

        (bootdevnum, rootdevnum, rootdev, prefix) = self._get_extlinux_boot_config()


        # Set MBR
        mbrsize = os.stat("%s/usr/share/syslinux/mbr.bin" % self._instroot)[stat.ST_SIZE]
        rc = subprocess.call(["/bin/dd", "if=%s/usr/share/syslinux/mbr.bin" % self._instroot, "of=" + loopdev])
        if rc != 0:
            raise MountError("Unable to set MBR to %s" % loopdev)

        # Set Bootable flag
        parted = "/usr/sbin/parted"
        if not os.path.exists(parted):
            parted = "/sbin/parted"
            if not os.path.exists(parted):
                raise CreatorError("Missed parted, please install it.")
        dev_null = os.open("/dev/null", os.O_WRONLY)
        rc = subprocess.call([parted, "-s", loopdev, "set", "%d" % (bootdevnum + 1), "boot", "on"],
                             stdout = dev_null, stderr = dev_null)
        os.close(dev_null)
        # XXX disabled return code check because parted always fails to
        # reload part table with loop devices. Annoying because we can't
        # distinguish this failure from real partition failures :-(
        if rc != 0 and 1 == 0:
            raise MountError("Unable to set bootable flag to %sp%d" % (loopdev, (bootdevnum + 1)))


        # Ensure all data is flushed to disk before doing extlinux install
        subprocess.call(["sync"])

        fullpathextlinux = "/sbin/extlinux"
        if not os.path.isfile(fullpathextlinux):
            fullpathextlinux = "/usr/sbin/extlinux"
            if not os.path.isfile(fullpathextlinux):
                fullpathextlinux = "/usr/bin/extlinux"
        rc = subprocess.call([fullpathextlinux, "-i", "%s/boot/extlinux" % self._instroot])
        if rc != 0:
            raise MountError("Unable to install extlinux bootloader to %sp)d" % (loopdev, (bootdevnum + 1)))


    def _create_bootconfig(self):
        logging.debug("Writing kickstart file.")
        self._write_kickstart()
        # For EC2 lets make a grub Legacy config file
        # (only if bootloader is enabled)
        if ((hasattr(self.ks.handler.bootloader, "disabled") and self.ks.handler.bootloader.disabled is False) and
           (hasattr(self.ks.handler.bootloader, "location") and self.ks.handler.bootloader.location != "none")):
            logging.debug("Writing GRUB Legacy config.")
            self._create_grub_config()

        if self.bootloader == 'grub2':
            # We have GRUB2 package installed
            # Most probably this is Fedora 16+
            logging.debug("Using GRUB2.")
            self._create_grub_devices(2)
            self._install_grub2()
        elif self.bootloader == 'grub':
            # We have GRUB Legacy installed
            logging.debug("Using GRUB Legacy.")
            self._create_grub_devices()
            self._copy_grub_files()
            self._install_grub()
        elif self.bootloader == 'extlinux':
            logging.debug("Using EXTLINUX.")
            self._create_extlinux_config()
            self._install_extlinux()
        elif self.bootloader == 'extlinux-bootloader':
            logging.debug("Using EXTLINUX from extlinux-bootloader package.")
            self._create_extlinux_config()
        else:
            # No GRUB package is available
            logging.warning("WARNING! No bootloader found.")

    def _unmount_instroot(self):
        if not self.__instloop is None:
            self.__instloop.cleanup()

    def _resparse(self, size = None):
        return self.__instloop.resparse(size)

    def package(self, destdir, package, include):
        """Prepares the created image for final delivery.
           Stage
           add includes
           package
        """
        self._stage_final_image()

        #add stuff
        if include and os.path.isdir(include):
            logging.debug("adding everything in %s to %s" % (include, self._outdir))
            files = glob.glob('%s/*' % include)
            for file in files:
                if os.path.isdir(file):
                    logging.debug("adding dir %s to %s" % (file, os.path.join(self._outdir, os.path.basename(file))))
                    shutil.copytree(file, os.path.join(self._outdir, os.path.basename(file)), symlinks=False)
                else:
                    logging.debug("adding %s to %s" % (file, self._outdir))
                    shutil.copy(file, self._outdir)
        elif include:
            logging.debug("adding %s to %s" % (include, self._outdir))
            shutil.copy(include, self._outdir)

        #package
        (pkg, comp) = os.path.splitext(package)
        if comp:
            comp = comp.lstrip(".")

        if pkg == "zip":
            dst = "%s/%s.zip" % (destdir, self.name)
            files = glob.glob('%s/*' % self._outdir)
            if comp == "64":
                logging.debug("creating %s with ZIP64 extensions" %  (dst))
                z = zipfile.ZipFile(dst, "w", compression=8, allowZip64="True")
            else:
                logging.debug("creating %s" %  (dst))
                z = zipfile.ZipFile(dst, "w", compression=8, allowZip64="False")
            for file in files:
                if file != dst:
                    if os.path.isdir(file):
                        #because zip sucks we cannot just add a dir
                        for root, dirs, dirfiles in os.walk(file):
                            for dirfile in dirfiles:
                                arcfile = self.name+"/"+root[len(os.path.commonprefix((os.path.dirname(file), root)))+1:]+"/"+dirfile
                                filepath = os.path.join(root, dirfile)
                                logging.debug("adding %s to %s" % (arcfile, dst))
                                z.write(filepath, arcfile, compress_type=None)
                    else:
                        logging.debug("adding %s to %s" % (os.path.join(self.name, os.path.basename(file)), dst))
                        z.write(file, arcname = os.path.join(self.name, os.path.basename(file)), compress_type=None)
            z.close()

        elif pkg == "tar":
            if comp:
                dst = "%s/%s.tar.%s" % (destdir, self.name, comp)
            else:
                dst = "%s/%s.tar" % (destdir, self.name)
            files = glob.glob('%s/*' % self._outdir)
            logging.debug("creating %s" %  (dst))
            tar = tarfile.open(dst, "w|"+comp)
            for file in files:
                logging.debug("adding %s to %s" % (file, dst))
                tar.add(file, arcname = os.path.join(self.name, os.path.basename(file)))
            tar.close()

        else:
            dst = os.path.join(destdir, self.name)
            logging.debug("creating destination dir: " + dst)
            makedirs(dst)
            for f in os.listdir(self._outdir):
                logging.debug("moving %s to %s" % (os.path.join(self._outdir, f), os.path.join(dst, f)))
                shutil.move(os.path.join(self._outdir, f), os.path.join(dst, f))
        print("Finished")

    def _stage_final_image(self):
        """Stage the final system image in _outdir.
           Convert disks
           write meta data
        """
        self._resparse()

        #if disk_format is not raw convert the disk and put in _outdir
        if self.__disk_format != "raw":
            self._convert_image()
        #else move to _outdir
        else:
            logging.debug("moving disks to stage location")
            for name in list(self.__disks.keys()):
                src = "%s/%s-%s.%s" % (self.__imgdir, self.name, name, self.__disk_format)
                dst = "%s/%s-%s.%s" % (self._outdir, self.name, name, self.__disk_format)

                if self.__compress:
                    # Compress with xz using 16 MiB block size for seekability
                    rc = subprocess.call(["xz", "-z", "--block-size=16777216", "-T 0", src])
                    if rc == 0:
                        logging.debug("compression successful")
                    if rc != 0:
                        raise CreatorError("Unable to compress disk to %s" % self.__disk_format)
                    src = "%s.xz" % (src)
                    dst = "%s.xz" % (dst)

                logging.debug("moving %s to %s" % (src, dst))
                shutil.move(src, dst)
        #write meta data in stage dir
        self._write_image_xml()

    def _convert_image(self):
        #convert disk format
        for name in list(self.__disks.keys()):
            dst = "%s/%s-%s.%s" % (self._outdir, self.name, name, self.__disk_format)
            logging.debug("converting %s image to %s" % (self.__disks[name].lofile, dst))
            if self.__compress and self.__disk_format == "qcow2":
                logging.debug("using compressed qcow2")
                compressflag = "-c"
            else:
                compressflag = ""
            rc = subprocess.call(["qemu-img", "convert", compressflag,
                                   "-f", "raw", self.__disks[name].lofile,
                                   "-O", self.__disk_format,  dst])
            if rc == 0:
                logging.debug("convert successful")
            if rc != 0:
                raise CreatorError("Unable to convert disk to %s" % self.__disk_format)

    def _write_kickstart(self):
        #write out the kicks tart to /root/anaconda-ks.cfg
        ks = open(self._instroot + "/root/anaconda-ks.cfg", "w")
        ks.write("%s" % (self.ks.handler,))
        ks.close()



    def _write_image_xml(self):
        xml = "<image>\n"

        name_attributes = ""
        if self.appliance_version:
            name_attributes += " version='%s'" % self.appliance_version
        if self.appliance_release:
            name_attributes += " release='%s'" % self.appliance_release
        xml += "  <name%s>%s</name>\n" % (name_attributes, self.name)
        xml += "  <domain>\n"
        # XXX don't hardcode - determine based on the kernel we installed for grub
        # baremetal vs xen
        xml += "    <boot type='hvm'>\n"
        xml += "      <guest>\n"
        xml += "        <arch>%s</arch>\n" % os.uname()[4]
        xml += "      </guest>\n"
        xml += "      <os>\n"
        xml += "        <loader dev='hd'/>\n"
        xml += "      </os>\n"

        i = 0
        for name in list(self.__disks.keys()):
            xml += "      <drive disk='%s-%s.%s' target='hd%s'/>\n" % (self.name, name, self.__disk_format, chr(ord('a')+i))
            i = i + 1

        xml += "    </boot>\n"
        xml += "    <devices>\n"
        xml += "      <vcpu>%s</vcpu>\n" % self.vcpu
        xml += "      <memory>%d</memory>\n" % (self.vmem * 1024)
        for network in self.ks.handler.network.network:
            xml += "      <interface/>\n"
        xml += "      <graphics/>\n"
        xml += "    </devices>\n"
        xml += "  </domain>\n"
        xml += "  <storage>\n"

        if self.checksum is True:
            for name in list(self.__disks.keys()):
                diskpath = "%s/%s-%s.%s" % (self._outdir, self.name, name, self.__disk_format)
                disk_size = os.path.getsize(diskpath)
                meter_ct = 0
                meter = progress.Bar("Generating disk signature for %s-%s.%s" % (self.name, name, self.__disk_format), max=disk_size)
                xml += "    <disk file='%s-%s.%s' use='system' format='%s'>\n" % (self.name, name, self.__disk_format, self.__disk_format)

                try:
                    import hashlib
                    m1 = hashlib.sha1()
                    m2 = hashlib.sha256()
                except:
                    import sha
                    m1 = sha.new()
                    m2 = None
                f = open(diskpath, "r")
                while 1:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    m1.update(chunk)
                    if m2:
                        m2.update(chunk)
                    meter.next(65536)

                sha1checksum = m1.hexdigest()
                xml +=  """      <checksum type='sha1'>%s</checksum>\n""" % sha1checksum

                if m2:
                    sha256checksum = m2.hexdigest()
                    xml += """      <checksum type='sha256'>%s</checksum>\n""" % sha256checksum
                xml += "    </disk>\n"
        else:
            for name in list(self.__disks.keys()):
                xml += "    <disk file='%s-%s.%s' use='system' format='%s'/>\n" % (self.name, name, self.__disk_format, self.__disk_format)

        xml += "  </storage>\n"
        xml += "</image>\n"

        logging.debug("writing image XML to %s/%s.xml" %  (self._outdir, self.name))
        cfg = open("%s/%s.xml" % (self._outdir, self.name), "w")
        cfg.write(xml)
        cfg.close()
        #print "Wrote: %s.xml" % self.name

