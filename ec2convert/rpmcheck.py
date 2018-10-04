# rpmcheck.py: Convert a virtual appliance image in an EC2 AMI, checks for rpm packages
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



import sys
import os
import logging

package_list = ['openssh-server','curl']

def checkpkgs(tmpdir):
    notinstalled_list = ""
    for package in package_list:
        rpm = os.popen("rpm -q --root=%s %s" % (tmpdir,package))
        rpm = rpm.read()
        if rpm.endswith("installed\n"):
            notinstalled_list += package
        else:
          logging.error("%s is installed\n" % rpm.strip())
        if notinstalled_list:
            logging.error("Package(s): %s not installed, exiting" % notinstalled_list)
            logging.error("Please install %s and rerun ec2-converter" % notinstalled_list)
            logging.error("Or add --rpmcheck=no option")
            return False
            
    return True
