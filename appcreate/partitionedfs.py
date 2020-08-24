#
# partitionedfs.py: partitioned files system class, extends fs.py
#
# Copyright 2007-2008, Red Hat  Inc.
# Copyright 2008, Daniel P. Berrange
# Copyright 2008,  David P. Huff
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
from builtins import str
from builtins import range
import os
import os.path
import glob
import shutil
import subprocess
import logging
import re

from imgcreate.errors import *
from imgcreate.fs import *


class PartitionedMount(Mount):
    def __init__(self, disks, mountdir, partition_layout, bootloader):
        Mount.__init__(self, mountdir)
        self.disks = {}
        for name in list(disks.keys()):
            self.disks[name] = { 'disk': disks[name],  # Disk object
                                 'mapped': False, # True if kpartx mapping exists
                                 'numpart': 0, # Number of allocate partitions
                                 'partitions': [], # indexes to self.partitions
                                 'extended': 0, # Size of extended partition
                                 'offset': 0 } # Offset of next partition

        self.partitions = []
        self.subvolumes = []
        self.mapped = False
        self.mountOrder = []
        self.unmountOrder = []
        self.partition_layout = partition_layout
        self.bootloader = bootloader
        self.has_extended = False # Has extended partition layout

    def add_partition(self, size, disk, mountpoint, fstype = None):
        self.partitions.append({'size': size,
                                'mountpoint': mountpoint, # Mount relative to chroot
                                'fstype': fstype,
                                'disk': disk,  # physical disk name holding partition
                                'device': None, # kpartx device node for partition
                                'mount': None, # Mount object
                                'UUID': None, # UUID for partition
                                'num': None}) # Partition number

    def add_subvolume(self, parent, mountpoint, name):
        self.subvolumes.append({'parent': parent, 'mountpoint': mountpoint, 'name': name})

    def __format_disks(self):
        logging.debug("Formatting disks")
        for dev in list(self.disks.keys()):
            d = self.disks[dev]
            logging.debug("Initializing partition table for %s with %s layout" % (d['disk'].device, self.partition_layout))
            rc = subprocess.call(["/sbin/parted", "-s", d['disk'].device, "mklabel", "%s" % self.partition_layout])
            if rc != 0:
                raise MountError("Error writing partition table on %s" % d['disk'].device)

        logging.debug("Assigning partitions to disks")
        for n in range(len(self.partitions)):
            p = self.partitions[n]

            if p['disk'] not in self.disks:
                raise MountError("No disk %s for partition %s" % (p['disk'], p['mountpoint']))

            d = self.disks[p['disk']]
            d['numpart'] += 1
            if d['numpart'] > 4 and self.partition_layout == 'msdos':
                # Increase allocation of extended partition to hold this partition
                d['extended'] += p['size']
                p['type'] = 'logical'
                p['num'] = d['numpart'] + 1
            else:
                p['type'] = 'primary'
                p['num'] = d['numpart']
            if d['offset'] == 0:
                d['offset'] = 4
            p['start'] = d['offset']
            d['offset'] += p['size']
            d['partitions'].append(n)
            logging.debug("Assigned %s to %s%d at %d at size %d" % (p['mountpoint'], p['disk'], p['num'], p['start'], p['size']))

        # XXX we should probably work in cylinder units to keep fdisk happier..
        start = 0
        logging.debug("Creating partitions")
        for p in self.partitions:
            d = self.disks[p['disk']]
            if p['num'] == 5 and self.partition_layout == 'msdos':
                self.has_extended = True
                logging.debug("Added extended part at %d of size %d" % (p['start'], d['extended']))
                rc = subprocess.call(["/sbin/parted", "-s", d['disk'].device, "mkpart", "extended",
                                      "%dM" % p['start'], "%dM" % (p['start'] + d['extended'])])

            logging.debug("Add %s part at %d of size %d" % (p['type'], p['start'], p['size']))
            p['originalfstype'] = p['fstype']
            if p['fstype'] == 'btrfs':
                fstype = 'btrfs'
            if p['fstype'].startswith('ext'):
                fstype = 'ext2'
            if p['fstype'].startswith('swap') or p['mountpoint'].startswith('swap'):
                fstype = 'linux-swap'
            if p['fstype'] == 'vfat':
                fstype = 'fat32'
            if p['fstype'] == 'efi':
                fstype = 'fat32'
                p['fstype'] = 'vfat'

            logging.debug("Part fstype: %s, effective fstype %s" % (p['fstype'], fstype))

            rc = subprocess.call(["/sbin/parted", "-a", "opt", "-s", d['disk'].device, "mkpart",
                                  p['type'], fstype, "%dM" % p['start'], "%dM" % (p['start']+p['size'])])

            # XXX disabled return code check because parted always fails to
            # reload part table with loop devices. Annoying because we can't
            # distinguish this failure from real partition failures :-(
            #if rc != 0 and 1 == 0:
            #    raise MountError("Error creating partition on %s" % d['disk'].device)

    def __map_partitions(self):
        for dev in list(self.disks.keys()):
            d = self.disks[dev]
            if d['mapped']:
                continue

            kpartx = subprocess.Popen(["/sbin/kpartx", "-l", d['disk'].device],
                                      stdout=subprocess.PIPE)

            kpartxOutput = kpartx.communicate()[0].decode("utf-8").split("\n")
            # Strip trailing blank
            kpartxOutput = kpartxOutput[0:len(kpartxOutput)-1]

            if kpartx.returncode:
                raise MountError("Failed to query partition mapping for '%s'" %
                                 d.device)

            # Pop the fourth (extended) partition
            if self.has_extended:
                kpartxOutput.pop(3)

            # Quick sanity check that the number of partitions matches
            # our expectation. If it doesn't, someone broke the code
            # further up
            if len(kpartxOutput) != d['numpart']:
                raise MountError("Unexpected number of partitions from kpartx: %d != %d" %
                                 (len(kpartxOutput), d['numpart']))

            for i in range(len(kpartxOutput)):
                line = kpartxOutput[i]
                newdev = line.split()[0]
                newdev_id = re.search('^loop\d+?p(\d+)$', newdev).group(1)

                mapperdev = "/dev/mapper/" + newdev
                loopdev = d['disk'].device + newdev_id

                logging.debug("Dev %s: %s -> %s" % (newdev, loopdev, mapperdev))
                pnum = d['partitions'][i]
                self.partitions[pnum]['device'] = loopdev
                self.partitions[pnum]['devicemapper'] = mapperdev

                # Loop devices are sometimes still left hanging around from untidily
                # terminated processes.
                logging.debug('Creating symlink from %s to %s', loopdev, mapperdev)
                try:
                    # grub's install wants partitions to be named
                    # to match their parent device + partition num
                    # kpartx doesn't work like this, so we add compat
                    # symlinks to point to /dev/mapper
                    os.symlink(mapperdev, loopdev)
                except OSError as e:
                    if e.errno == errno.EEXIST:
                        os.unlink(loopdev)
                        os.symlink(mapperdev, loopdev)
                    else:
                        raise

            logging.debug("Adding partx mapping for %s" % d['disk'].device)
            rc = subprocess.call(["/sbin/kpartx", "-a", "-s", d['disk'].device])
            if rc != 0:
                raise MountError("Failed to map partitions for '%s'" %
                                 d['disk'].device)
            d['mapped'] = True


    def __unmap_partitions(self):
        for dev in list(self.disks.keys()):
            d = self.disks[dev]
            if not d['mapped']:
                continue

            logging.debug("Removing compat symlinks")
            for pnum in d['partitions']:
                if self.partitions[pnum]['device'] != None:
                    os.unlink(self.partitions[pnum]['device'])
                    self.partitions[pnum]['device'] = None

            logging.debug("Unmapping %s" % d['disk'].device)
            rc = subprocess.call(["/sbin/kpartx", "-d", d['disk'].device])
            if rc != 0:
                raise MountError("Failed to unmap partitions for '%s'" %
                                 d['disk'].device)

            d['mapped'] = False


    def __calculate_mountorder(self):
        btrfs_mountOrder = []
        btrfs_unmountOrder = []
        for p in self.partitions:
            if p['fstype'] == 'btrfs':
                btrfs_mountOrder.append(p['mountpoint'])
                btrfs_unmountOrder.append(p['mountpoint'])
            else:
                self.mountOrder.append(p['mountpoint'])
                self.unmountOrder.append(p['mountpoint'])

        self.mountOrder.sort()
        btrfs_mountOrder.sort()
        # Btrfs mountpoints must be first in the list
        self.mountOrder = btrfs_mountOrder + self.mountOrder
        self.unmountOrder.sort()
        self.unmountOrder.reverse()
        btrfs_unmountOrder.sort()
        btrfs_unmountOrder.reverse()
        # Btrfs mountpoints must be last in the list
        self.unmountOrder = self.unmountOrder + btrfs_unmountOrder
        logging.debug("Mount order: %s" % str(self.mountOrder))
        logging.debug("Unmount order: %s" % str(self.unmountOrder))

    def cleanup(self):
        Mount.cleanup(self)
        self.__unmap_partitions()
        for dev in list(self.disks.keys()):
            d = self.disks[dev]
            try:
                d['disk'].cleanup()
            except:
                pass

    def unmount(self):
        for mp in self.unmountOrder:
            if mp == 'swap':
                continue
            p = None
            for p1 in self.partitions:
                if p1['mountpoint'] == mp:
                    p = p1
                    break

            if p['mount'] != None:
                try:
                    p['mount'].cleanup()
                except:
                    pass
                p['mount'] = None

        if self.subvolumes:
            ordered = []
            others = []
            for s in self.subvolumes:
                if s['mountpoint'] == '/':
                    others.append(s)
                else:
                    ordered.append(s)

            ordered = ordered + others

            for s in ordered:
                logging.info("Unmounting directory %s%s" % (self.mountdir, s['mountpoint']))
                rc = subprocess.call(['umount', "%s%s" % (self.mountdir, s['mountpoint'])])
                if rc != 0:
                    logging.warning("Unmounting directory %s%s failed, using lazy "
                                 "umount" % (self.mountdir, s['mountpoint']))
                    print("Unmounting directory %s%s failed, using lazy umount" %
                          (self.mountdir, s['mountpoint']), file=sys.stdout)
                    subprocess.call(['umount', '-l', "%s%s" % (self.mountdir, s['mountpoint'])])

    def setup_subvolumes(self):
        others = []
        ordered = []
        for s in self.subvolumes:
            if s['mountpoint'] == '/':
                ordered.append(s)
            else:
                others.append(s)
  
        ordered += others
        for s in ordered:
            base = "%s%s" % (self.mountdir, s['parent'])
            path = "%s/%s" % (base, s['name'])
            mountpath = "%s%s" % (self.mountdir, s['mountpoint'])
            logging.debug("Creating Btrfs subvolume at path %s" % path)
            subprocess.call(['btrfs', 'subvol', 'create', path])
            subprocess.call(['mkdir', '-p', mountpath])
            logging.debug("Mounting Btrfs subvolume %s at path %s" % (s['name'], mountpath))
            device = subprocess.Popen(["findmnt", "-n", "-o", "SOURCE", base], stdout=subprocess.PIPE).communicate()[0].decode("utf-8").strip()
            subprocess.call(['mount', '-t', 'btrfs', '-o', 'subvol=%s' % s['name'], device, mountpath])

    def mount(self):
        for dev in list(self.disks.keys()):
            d = self.disks[dev]
            d['disk'].create()

        self.__format_disks()
        self.__map_partitions()
        self.__calculate_mountorder()

        boot_flag_mp = "/boot"
        if "/boot/efi" in self.mountOrder and self.bootloader == "grub2":
            boot_flag_mp = "/boot/efi"


        for mp in self.mountOrder:
            p = None
            for p1 in self.partitions:
                if p1['mountpoint'] == mp:
                    p = p1
                    break

            if mp == '/boot/uboot':
                subprocess.call(["/bin/mkdir", "-p", "%s%s" % (self.mountdir, p['mountpoint'])])
                # mark the partition bootable
                subprocess.call(["/sbin/parted", "-s", self.disks[p['disk']]['disk'].device, "set", str(p['num']), "boot", "on"])
                subprocess.call(["/sbin/mkfs.vfat", "-n", "uboot", p['device']])
                p['UUID'] = self.__getuuid(p['device'])
                continue

            if mp == boot_flag_mp:
                logging.debug("Setting boot flag on in %s" % mp)
                # mark the partition bootable
                subprocess.call(["/sbin/parted", "-s", self.disks[p['disk']]['disk'].device, "set", str(p['num']), "boot", "on"])

            if p['originalfstype'] == 'efi' and self.partition_layout == 'msdos':
                logging.debug("Setting esp flag on in %s" % mp)
                # mark the partition as efi, may fail on older versions of parted, like the ones in CentOS 7
                rc = subprocess.call(["/sbin/parted", "-s", self.disks[p['disk']]['disk'].device, "set", str(p['num']), "esp", "on"])
                if rc != 0:
                    logging.debug("parted failed setting esp flag, trying sfdisk")
                    subprocess.call(["/sbin/sfdisk", "--change-id", self.disks[p['disk']]['disk'].device, str(p['num']), "ef"])

            if mp == 'biosboot':
                subprocess.call(["/sbin/parted", "-s", self.disks[p['disk']]['disk'].device, "set", "1", "bios_grub", "on"])
                continue

            if mp == 'swap':
                subprocess.call(["/sbin/mkswap", "-L", "_swap", p['device']])
                p['UUID'] = self.__getuuid(p['device'])
                continue

            rmmountdir = False
            if p['mountpoint'] == "/":
                rmmountdir = True
            # despite the name, this supports btrfs too
            pdisk = ExtDiskMount(RawDisk(p['size'] * 1024 * 1024, p['device']),
                                 self.mountdir + p['mountpoint'],
                                 p['fstype'],
                                 4096,
                                 p['mountpoint'],
                                 rmmountdir)
            pdisk.mount()
            if p['fstype'] == 'btrfs':
                self.setup_subvolumes()
            p['mount'] = pdisk
            p['UUID'] = self.__getuuid(p['device'])

    def resparse(self, size = None):
        # Can't re-sparse a disk image - too hard
        pass

    def __getuuid(self, partition):
        devdata = subprocess.Popen(["/sbin/blkid", partition], stdout=subprocess.PIPE)
        devdataout = devdata.communicate()[0].decode("utf-8").split()
        for data in devdataout:
            if data.startswith("UUID"):
                UUID = data.replace('"', '')
                continue
        return UUID

