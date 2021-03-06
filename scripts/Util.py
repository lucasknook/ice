# **********************************************************************
#
# Copyright (c) 2003-2018 ZeroC, Inc. All rights reserved.
#
# This copy of Ice is licensed to you under the terms described in the
# ICE_LICENSE file included in this distribution.
#
# **********************************************************************

import os, sys, runpy, getopt, traceback, types, threading, time, datetime, re, itertools, random, subprocess, shutil, copy, inspect

import xml.sax.saxutils

isPython2 = sys.version_info[0] == 2
if isPython2:
    import Queue as queue
    from StringIO import StringIO
else:
    import queue
    from io import StringIO

from collections import OrderedDict
import Expect

toplevel = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

def run(cmd, cwd=None, err=False):
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd)
    try:
        out = p.stdout.read().decode('UTF-8').strip()
        if(not err and p.wait() != 0) or (err and p.wait() == 0) :
            raise RuntimeError(cmd + " failed:\n" + out)
    finally:
        #
        # Without this we get warnings when running with python_d on Windows
        #
        # ResourceWarning: unclosed file <_io.TextIOWrapper name=3 encoding='cp1252'>
        #
        p.stdout.close()
    return out

def val(v, escapeQuotes=False, quoteValue=True):
    if type(v) == bool:
        return "1" if v else "0"
    elif type(v) == str:
        if not quoteValue or v.find(" ") < 0:
            return v
        elif escapeQuotes:
            return "\\\"{0}\\\"".format(v.replace("\\\"", "\\\\\\\""))
        else:
            return "\"{0}\"".format(v)
    else:
        return str(v)

illegalXMLChars = re.compile(u'[\x00-\x08\x0b\x0c\x0e-\x1F\uD800-\uDFFF\uFFFE\uFFFF]')

def escapeXml(s, attribute=False):
    # Remove backspace characters from the output (they aren't accepted by Jenkins XML parser)
    if isPython2:
        s = "".join(ch for ch in unicode(s.decode("utf-8")) if ch != u"\u0008").encode("utf-8")
    else:
        s = "".join(ch for ch in s if ch != u"\u0008")
    s = illegalXMLChars.sub("?", s) # Strip invalid XML characters
    return xml.sax.saxutils.quoteattr(s) if attribute else xml.sax.saxutils.escape(s)

def getIceSoVersion():
    config = open(os.path.join(toplevel, "cpp", "include", "IceUtil", "Config.h"), "r")
    intVersion = int(re.search("ICE_INT_VERSION ([0-9]*)", config.read()).group(1))
    majorVersion = int(intVersion / 10000)
    minorVersion = int(intVersion / 100) - 100 * majorVersion
    patchVersion = intVersion % 100
    if patchVersion < 50:
        return '%d' % (majorVersion * 10 + minorVersion)
    elif patchVersion < 60:
        return '%da%d' % (majorVersion * 10 + minorVersion, patchVersion - 50)
    else:
        return '%db%d' % (majorVersion * 10 + minorVersion, patchVersion - 60)

class Platform:

    def __init__(self):
        self.parseBuildVariables({
            "supported-platforms" : ("supportedPlatforms", lambda s : s.split(" ")),
            "supported-configs" : ("supportedConfigs", lambda s : s.split(" "))
        })

        try:
            run("dotnet --version")
            self.nugetPackageCache = re.search("info : global-packages: (.*)",
                                               run("dotnet nuget locals --list global-packages")).groups(1)[0]
        except:
            self.nugetPackageCache = None

    def parseBuildVariables(self, variables):
        # Run make to get the values of the given variables
        output = run('make print V="{0}"'.format(" ".join(variables.keys())), cwd = toplevel)
        for l in output.split("\n"):
            match = re.match(r'^.*:.*: (.*) = (.*)', l)
            if match and match.group(1):
                if match.group(1) in variables:
                    (varname, valuefn) = variables[match.group(1).strip()]
                    value = match.group(2).strip() or ""
                    setattr(self, varname, valuefn(value) if valuefn else value)

    def getFilters(self, config):
        if config.buildConfig in ["static", "cpp11-static"]:
            return (["Ice/.*", "IceSSL/configuration", "IceDiscovery/simple", "IceGrid/simple", "Glacier2/application"],
                    ["Ice/library", "Ice/plugin"])
        return ([], [])

    def getDefaultBuildPlatform(self):
        return self.supportedPlatforms[0]

    def getDefaultBuildConfig(self):
        return self.supportedConfigs[0]

    def getBinSubDir(self, mapping, process, current):
        # Return the bin sub-directory for the given mapping,platform,config,
        # to be overriden by specializations
        return "bin"

    def getLibSubDir(self, mapping, process, current):
        # Return the bin sub-directory for the given mapping,platform,config,
        # to be overriden by specializations
        return "lib"

    def getBuildSubDir(self, mapping, name, current):
        # Return the build sub-directory, to be overriden by specializations
        buildPlatform = current.driver.configs[mapping].buildPlatform
        buildConfig = current.driver.configs[mapping].buildConfig
        return os.path.join("build", buildPlatform, buildConfig)

    def getLdPathEnvName(self):
        return "LD_LIBRARY_PATH"

    def getInstallDir(self, mapping, current, envName):
        return os.environ.get(envName, "/usr")

    def getIceInstallDir(self, mapping, current):
        #
        # For .NET Core with Ice binary distribution the NuGet packages is
        # installed in the global packages package cache
        #
        if isinstance(mapping, CSharpMapping) and current.driver.useIceBinDist(mapping) and current.config.netframework:
            return os.path.join(self.nugetPackageCache, "zeroc.ice.net", self.getNugetPackageVersion(mapping))
        else:
            return self.getInstallDir(mapping, current, "ICE_HOME")

    def getSliceDir(self, iceDir):
        if iceDir.startswith("/usr"):
            return os.path.join(iceDir, "share", "ice", "slice")
        else:
            return os.path.join(iceDir, "slice")

    def getDefaultExe(self, name, config):
        if name == "icebox" and config.cpp11:
            name += "++11"
        return name

    def hasOpenSSL(self):
        # This is used by the IceSSL test suite to figure out how to setup certificates
        return False

    def canRun(self, mapping, current):
        return True

    def getDotnetExe(self):
        return "dotnet"

    def getNugetPackageVersion(self, mapping):
        version = None
        if isinstance(mapping, CSharpMapping):
            with open(os.path.join(toplevel, "csharp", "msbuild", "zeroc.ice.net.nuspec"), "r") as configFile:
                version = re.search("<version>(.*)</version>", configFile.read()).group(1)
        else:
            with open(os.path.join(toplevel, "config", "icebuilder.props"), "r") as configFile:
                version = re.search("<IceJSONVersion>(.*)</IceJSONVersion>", configFile.read()).group(1)
        return version

class Darwin(Platform):

    def getFilters(self, config):
        if config.buildPlatform in ["iphoneos", "iphonesimulator", "macosx"] and "xcodesdk" in config.buildConfig:
            return (["Ice/.*", "IceSSL/configuration"],
                    ["Ice/background",
                     "Ice/echo",
                     "Ice/faultTolerance",
                     "Ice/gc",
                     "Ice/library",
                     "Ice/logger",
                     "Ice/properties",
                     "Ice/plugin",
                     "Ice/stringConverter",
                     "Ice/threadPoolPriority",
                     "Ice/udp"])
        return Platform.getFilters(self, config)

    def getDefaultBuildPlatform(self):
        return "macosx"

    def getLdPathEnvName(self):
        return "DYLD_LIBRARY_PATH"

    def getInstallDir(self, mapping, current, envName):
        return os.environ.get(envName, "/usr/local")

class AIX(Platform):

    def hasOpenSSL(self):
        return True

class Linux(Platform):

    def __init__(self):
        Platform.__init__(self)
        self.parseBuildVariables({
            "linux_id" : ("linuxId", None),
            "build-platform" : ("buildPlatform", None),
            "foreign-platforms" : ("foreignPlatforms", lambda s : s.split(" ") if s else []),
        })
        self.multiArch = {}
        if self.linuxId in ["ubuntu", "debian"]:
            for p in [self.buildPlatform] + self.foreignPlatforms:
                self.multiArch[p] = run("dpkg-architecture -f -a{0} -qDEB_HOST_MULTIARCH 2> /dev/null".format(p))

    def hasOpenSSL(self):
        return True

    def getBinSubDir(self, mapping, process, current):
        buildPlatform = current.driver.configs[mapping].buildPlatform
        if self.linuxId in ["ubuntu", "debian"] and buildPlatform in self.foreignPlatforms:
            return os.path.join("bin", self.multiArch[buildPlatform])
        return "bin"

    def getLibSubDir(self, mapping, process, current):
        buildPlatform = current.driver.configs[mapping].buildPlatform

        # PHP module is always installed in the lib directory for the default build platform
        if isinstance(mapping, PhpMapping) and buildPlatform == self.getDefaultBuildPlatform():
            return "lib"

        if self.linuxId in ["centos", "rhel", "fedora"]:
            return "lib64" if buildPlatform == "x64" else "lib"
        elif self.linuxId in ["ubuntu", "debian"]:
            return os.path.join("lib", self.multiArch[buildPlatform])
        return "lib"

    def getBuildSubDir(self, mapping, name, current):
        buildPlatform = current.driver.configs[mapping].buildPlatform
        buildConfig = current.driver.configs[mapping].buildConfig
        if self.linuxId in ["ubuntu", "debian"]:
            return os.path.join("build", self.multiArch[buildPlatform], buildConfig)
        else:
            return os.path.join("build", buildPlatform, buildConfig)

    def getDefaultExe(self, name, config):
        if name == "icebox":
            if self.linuxId in ["centos", "rhel", "fedora"] and config.buildPlatform == "x86":
                name += "32" # Multilib platform
            if config.cpp11:
                name += "++11"
        return name

    def canRun(self, mapping, current):
        if self.linuxId in ["centos", "rhel", "fedora"] and current.config.buildPlatform == "x86":
            #
            # Don't test Glacier2/IceStorm/IceGrid services with multilib platforms. We only
            # build services for the native platform.
            #
            parent = re.match(r'^([\w]*).*', current.testcase.getTestSuite().getId()).group(1)
            if parent in ["Glacier2", "IceStorm", "IceGrid"]:
                return False
        return Platform.canRun(self, mapping, current)

class Windows(Platform):

    def __init__(self):
        Platform.__init__(self)
        self.compiler = None

    def getFilters(self, config):
        if config.uwp:
            return (["cpp/Ice/.*", "cpp/IceSSL/configuration"],
                    ["Ice/background",
                     "Ice/echo",
                     "Ice/faultTolerance",
                     "Ice/gc",
                     "Ice/library",
                     "Ice/logger",
                     "Ice/networkProxy",        # SOCKS proxy not supported with UWP
                     "Ice/properties",          # Property files are not supported with UWP
                     "Ice/plugin",
                     "Ice/threadPoolPriority"])
        elif self.getCompiler() in ["VC100"]:
            return (["cpp/Ice/.*",
                     "cpp/IceSSL/.*",
                     "cpp/IceBox/.*",
                     "cpp/IceDiscovery/.*",
                     "cpp/IceUtil/.*",
                     "cpp/Slice/.*"], [])
        else:
            iceBinDist = os.environ.get("ICE_BIN_DIST", "").split()
            exclude = ["ruby"]
            if not 'all' in iceBinDist:
                if not 'python' in iceBinDist and not self.getCompiler() in ["VC140"]:
                    exclude.append("python")
                if not 'php' in iceBinDist and not self.getCompiler() in ["VC140"]:
                    exclude.append("php")
            return ([], exclude)

    def parseBuildVariables(self, variables):
        pass # Nothing to do, we don't support the make build system on Windows

    def getDefaultBuildPlatform(self):
        return "x64" if "X64" in os.environ.get("PLATFORM", "") else "Win32"

    def getDefaultBuildConfig(self):
        return "Release"

    def getCompiler(self):
        if self.compiler != None:
            return self.compiler
        if os.environ.get("CPP_COMPILER", "") != "":
            self.compiler = os.environ["CPP_COMPILER"]
        else:
            try:
                out = run("cl")
                if out.find("Version 16.") != -1:
                    self.compiler = "VC100"
                elif out.find("Version 17.") != -1:
                    self.compiler = "VC110"
                elif out.find("Version 18.") != -1:
                    self.compiler = "VC120"
                elif out.find("Version 19.00.") != -1:
                    self.compiler = "VC140"
                elif out.find("Version 19.1") != -1:
                    self.compiler = "VC141"
                else:
                    raise RuntimeError("Unknown compiler version:\n{0}".format(out))
            except:
                self.compiler = ""
        return self.compiler

    def getPlatformToolset(self):
        return self.getCompiler().replace("VC", "v")

    def getBinSubDir(self, mapping, process, current):
        #
        # Platform/Config taget bin directories.
        #
        platform = current.driver.configs[mapping].buildPlatform
        buildConfig = current.driver.configs[mapping].buildConfig
        config = "Debug" if buildConfig.find("Debug") >= 0 else "Release"

        if current.config.uwp and not current.config.protocol in ["ssl", "wss"]:
            return ""
        elif current.driver.useIceBinDist(mapping):
            version = self.getNugetPackageVersion(mapping)
            packageSuffix = self.getPlatformToolset() if isinstance(mapping, CppMapping) else "net"
            package = os.path.join(mapping.path, "msbuild", "packages", "{0}".format(
                mapping.getNugetPackage(packageSuffix, version))) if hasattr(mapping, "getNugetPackage") else None
            nuget = package and os.path.exists(package)
            iceHome = os.environ.get("ICE_HOME", "")
            if isinstance(mapping, CppMapping) and not nuget and not os.path.exists(iceHome):
                raise RuntimeError("Cannot detect a valid C++ distribution")
            if not nuget:
                return "bin"
            elif isinstance(mapping, CSharpMapping) or isinstance(process, SliceTranslator):
                return os.path.join("tools")
            else:

                #
                # With Windows binary distribution some binaries are only included for Release configuration.
                #
                binaries = [Glacier2Router, IcePatch2Calc, IcePatch2Client, IcePatch2Server, IceBoxAdmin, IceBridge,
                            IceStormAdmin, IceGridAdmin]
                config = next(("Release" for p in binaries if isinstance(process, p)), config)

                return os.path.join("build", "native", "bin", platform, config)
        else:
            if isinstance(mapping, CppMapping):
                return os.path.join("bin", platform, config)
            elif isinstance(mapping, PhpMapping):
                return os.path.join("msbuild", "packages", "zeroc.ice.v140.{0}".format(self.getNugetPackageVersion(mapping)),
                                    "build", "native", "bin", platform, config)
            return "bin"

    def getLibSubDir(self, mapping, process, current):
        if isinstance(mapping, PhpMapping):
            return "php" if current.driver.useIceBinDist(mapping) else "lib"
        return self.getBinSubDir(mapping, process, current)

    def getBuildSubDir(self, mapping, name, current):
        buildPlatform = current.driver.configs[mapping].buildPlatform
        buildConfig = current.driver.configs[mapping].buildConfig
        if os.path.exists(os.path.join(current.testcase.getPath(), "msbuild", name)):
            return os.path.join("msbuild", name, buildPlatform, buildConfig)
        else:
            return os.path.join("msbuild", buildPlatform, buildConfig)

    def getLdPathEnvName(self):
        return "PATH"

    def getInstallDir(self, mapping, current, envName):

        platform = current.config.buildPlatform
        config = "Debug" if current.config.buildConfig.find("Debug") >= 0 else "Release"
        version = self.getNugetPackageVersion(mapping)
        packageSuffix = self.getPlatformToolset() if isinstance(mapping, CppMapping) else "net"
        package = None

        if isinstance(mapping, CSharpMapping) and current.config.netframework:
            #
            # Use NuGet package from nuget locals
            #
            package = os.path.join(self.nugetPackageCache, "zeroc.ice.net", version)
        elif hasattr(mapping, "getNugetPackage"):
            package = os.path.join(mapping.path, "msbuild", "packages", mapping.getNugetPackage(packageSuffix, version))

        home = os.environ.get(envName, "")
        if isinstance(mapping, CppMapping) and not package and not os.path.exists(home):
            raise RuntimeError("Cannot detect a valid C++ distribution")

        return package if package and os.path.exists(package) else home

    def canRun(self, mapping, current):
        #
        # On Windows, if testing with a binary distribution, don't test Glacier2/IceBridge services
        # with the Debug configurations since we don't provide binaries for them.
        #
        if current.driver.useIceBinDist(mapping):
            parent = re.match(r'^([\w]*).*', current.testcase.getTestSuite().getId()).group(1)
            if parent in ["Glacier2", "IceBridge"] and current.config.buildConfig.find("Debug") >= 0:
                return False
        return Platform.canRun(self, mapping, current)

    def getDotnetExe(self):
        try:
            return run("where dotnet").strip()
        except:
            return None

platform = None
if sys.platform == "darwin":
    platform = Darwin()
elif sys.platform.startswith("aix"):
    platform = AIX()
elif sys.platform.startswith("linux") or sys.platform.startswith("gnukfreebsd"):
    platform = Linux()
elif sys.platform == "win32" or sys.platform[:6] == "cygwin":
    platform = Windows()

if not platform:
    print("can't run on unknown platform `{0}'".format(sys.platform))
    sys.exit(1)

def parseOptions(obj, options, mapped={}):
    # Transform configuration options provided on the command line to
    # object data members. The data members must be already set on the
    # object and with the correct type.
    if not hasattr(obj, "parsedOptions"):
        obj.parsedOptions=[]
    remaining = []
    for (o, a) in options:
        if o.startswith("--"): o = o[2:]
        if o.startswith("-"): o = o[1:]
        if not a and o.startswith("no-"):
            a = "false"
            o = o[3:]
        if o in mapped:
            o = mapped[o]

        if hasattr(obj, o):
            if isinstance(getattr(obj, o), bool):
                setattr(obj, o, True if not a else (a.lower() in ["yes", "true", "1"]))
            elif isinstance(getattr(obj, o), list):
                l = getattr(obj, o)
                l.append(a)
            else:
                if not a and not isinstance(a, str):
                    a = "0"
                setattr(obj, o, type(getattr(obj, o))(a))
            if not o in obj.parsedOptions:
                obj.parsedOptions.append(o)
        else:
            remaining.append((o, a))
    options[:] = remaining

class Mapping:

    mappings = OrderedDict()

    class Config:

        # All option values for Ice/IceBox tests.
        coreOptions = {
            "protocol" : ["tcp", "ssl", "wss", "ws"],
            "compress" : [False, True],
            "ipv6" : [False, True],
            "serialize" : [False, True],
            "mx" : [False, True],
        }

        # All option values for IceGrid/IceStorm/Glacier2/IceDiscovery tests.
        serviceOptions = {
            "protocol" : ["tcp", "wss"],
            "compress" : [False, True],
            "ipv6" : [False, True],
            "serialize" : [False, True],
            "mx" : [False, True],
        }

        @classmethod
        def getSupportedArgs(self):
            return ("", ["config=", "platform=", "protocol=", "compress", "ipv6", "no-ipv6", "serialize", "mx",
                         "cprops=", "sprops="])

        @classmethod
        def usage(self):
            pass

        @classmethod
        def commonUsage(self):
            print("")
            print("Mapping options:")
            print("--protocol=<prot>     Run with the given protocol.")
            print("--compress            Run the tests with protocol compression.")
            print("--ipv6                Use IPv6 addresses.")
            print("--serialize           Run with connection serialization.")
            print("--mx                  Run with metrics enabled.")
            print("--cprops=<properties> Specifies a list of additional client properties.")
            print("--sprops=<properties> Specifies a list of additional server properties.")
            print("--config=<config>     Build configuration for native executables.")
            print("--platform=<platform> Build platform for native executables.")

        def __init__(self, options=[]):
            # Build configuration
            self.parsedOptions = []
            self.buildConfig = os.environ.get("CONFIGS", "").split(" ")[0]
            if self.buildConfig:
                self.parsedOptions.append("buildConfig")
            else:
                self.buildConfig = platform.getDefaultBuildConfig()

            self.buildPlatform = os.environ.get("PLATFORMS", "").split(" ")[0]
            if self.buildPlatform:
                self.parsedOptions.append("buildPlatform")
            else:
                self.buildPlatform = platform.getDefaultBuildPlatform()

            self.protocol = "tcp"
            self.compress = False
            self.serialize = False
            self.ipv6 = False
            self.mx = False
            self.cprops = []
            self.sprops = []
            parseOptions(self, options, { "config" : "buildConfig",
                                          "platform" : "buildPlatform" })

            # Options bellow are not parsed by the base class by still
            # initialized here for convenience (this avoid having to
            # check the configuration type)
            self.uwp = False
            self.openssl = False

            self.device = ""
            self.avd = ""
            self.androidemulator = False
            self.netframework = ""

        def __str__(self):
            s = []
            for o in self.parsedOptions:
                v = getattr(self, o)
                if v: s.append(o if type(v) == bool else str(v))
            return ",".join(s)

        def getAll(self, current, testcase, rand=False):

            #
            # A generator to generate combinations of options (e.g.: tcp/compress/mx, ssl/ipv6/serialize, etc)
            #
            def gen(supportedOptions):

                if not supportedOptions:
                    yield self
                    return

                supportedOptions = supportedOptions.copy()
                supportedOptions.update(testcase.getMapping().getOptions(current))
                supportedOptions.update(testcase.getTestSuite().getOptions(current))
                supportedOptions.update(testcase.getOptions(current))

                for o in self.parsedOptions:
                    # Remove options which were explicitly set
                    if o in supportedOptions:
                        del supportedOptions[o]

                if len(supportedOptions) == 0:
                    yield self
                    return

                # Find the option with the longest list of values
                length = max([len(v) for v in supportedOptions.values()])

                # Replace the values with a cycle iterator on the values
                for (k, v) in supportedOptions.items():
                    supportedOptions[k] = itertools.cycle(random.sample(v, len(v)) if rand else v)

                # Now, for the length of the longest array of values, we return
                # an array with the supported option combinations
                for i in range(0, length):
                    options = []
                    for k, v in supportedOptions.items():
                        v = next(v)
                        if v:
                            if type(v) == bool:
                                if v:
                                    options.append(("--{0}".format(k), None))
                            else:
                                options.append(("--{0}".format(k), v))

                    # Add parsed options
                    for o in self.parsedOptions:
                        v = getattr(self, o)
                        if type(v) == bool:
                            if v:
                                options.append(("--{0}".format(o), None))
                        elif type(v) == list:
                            options += [("--{0}".format(o), e) for e in v]
                        else:
                            options.append(("--{0}".format(o), v))

                    yield self.__class__(options)

            options = None
            parent = re.match(r'^([\w]*).*', testcase.getTestSuite().getId()).group(1)
            if isinstance(testcase, ClientServerTestCase) and parent in ["Ice", "IceBox"]:
                options = current.driver.filterOptions(testcase, self.coreOptions)
            elif parent in ["IceGrid", "Glacier2", "IceStorm", "IceDiscovery", "IceBridge"]:
                options = current.driver.filterOptions(testcase, self.serviceOptions)

            return [c for c in gen(options)]

        def canRun(self, current):
            if not platform.canRun(current.testcase.getMapping(), current):
                return False

            options = {}
            options.update(current.testcase.getMapping().getOptions(current))
            options.update(current.testcase.getTestSuite().getOptions(current))
            options.update(current.testcase.getOptions(current))

            for (k, v) in options.items():
                if hasattr(self, k):
                    if not getattr(self, k) in v:
                        return False
                elif hasattr(current.driver, k):
                    if not getattr(current.driver, k) in v:
                        return False
            else:
                return True

        def cloneRunnable(self, current):
            #
            # Clone this configuration and make sure all the options are supported
            #
            options = {}
            options.update(current.testcase.getMapping().getOptions(current))
            options.update(current.testcase.getTestSuite().getOptions(current))
            options.update(current.testcase.getOptions(current))
            clone = copy.copy(self)
            for o in self.parsedOptions:
                if o in options and getattr(self, o) not in options[o]:
                    setattr(clone, o, options[o][0] if len(options[o]) > 0 else None)
            return clone

        def cloneAndOverrideWith(self, current):
            #
            # Clone this configuration and override options with options from the given configuration
            # (the parent configuraton). This is usefull when running cross-testing. For example, JS
            # tests don't support all the options so we clone the C++ configuration and override the
            # options that are set on the JS configuration.
            #
            clone = copy.copy(self)
            for o in current.config.parsedOptions + ["protocol"]:
                if o not in ["buildConfig", "buildPlatform"]:
                    setattr(clone, o, getattr(current.config, o))
            clone.parsedOptions = current.config.parsedOptions
            return clone

        def getArgs(self, process, current):
            return []

        def getProps(self, process, current):
            props = {}
            if isinstance(process, IceProcess):
                props["Ice.Warn.Connections"] = True
                if self.protocol:
                    props["Ice.Default.Protocol"] = self.protocol
                if self.compress:
                    props["Ice.Override.Compress"] = "1"
                if self.serialize:
                    props["Ice.ThreadPool.Server.Serialize"] = "1"
                props["Ice.IPv6"] = self.ipv6
                if self.ipv6:
                    props["Ice.PreferIPv6Address"] = True
                if self.mx:
                    props["Ice.Admin.Endpoints"] = "default -h localhost"
                    props["Ice.Admin.InstanceName"] = "Server" if isinstance(process, Server) else "Client"
                    props["IceMX.Metrics.Debug.GroupBy"] ="id"
                    props["IceMX.Metrics.Parent.GroupBy"] = "parent"
                    props["IceMX.Metrics.All.GroupBy"] = "none"

                #
                # Speed up Windows testing. We override the connect timeout for some tests which are
                # establishing connections to inactive ports. It takes around 1s for such connection
                # establishment to fail on Windows.
                #
                # if isinstance(platform, Windows):
                #     if current.testsuite.getId().startswith("IceGrid") or \
                #         current.testsuite.getId() in ["Ice/binding",
                #                                       "Ice/location",
                #                                       "Ice/background",
                #                                       "Ice/faultTolerance",
                #                                       "Ice/services",
                #                                       "IceDiscovery/simple"]:
                #         props["Ice.Override.ConnectTimeout"] = "400"

                # Additional properties specified on the command line with --cprops or --sprops
                additionalProps = []
                if self.cprops and isinstance(process, Client):
                    additionalProps = self.cprops
                elif self.sprops and isinstance(process, Server):
                    additionalProps = self.sprops
                for pps in additionalProps:
                    for p in pps.split(" "):
                        if p.find("=") > 0:
                            (k , v) = p.split("=")
                            props[k] = v
                        else:
                            props[p] = True

            return props

    @classmethod
    def getByName(self, name):
        if not name in self.mappings:
            raise RuntimeError("unknown mapping `{0}'".format(name))
        return self.mappings.get(name)

    @classmethod
    def getByPath(self, path):
        path = os.path.abspath(path)
        mapping = None
        for m in self.mappings.values():
            if path.startswith(m.getPath() + os.sep) and (not mapping or len(mapping.getPath()) < len(m.getPath())):
                mapping=m
        return mapping

    @classmethod
    def add(self, name, mapping):
        name = name.replace("\\", "/")
        self.mappings[name] = mapping.init(name)

    @classmethod
    def getAll(self, driver=None):
        return [m for m in self.mappings.values() if not driver or driver.matchLanguage(str(m))]

    def __init__(self, path=None):
        self.platform = None
        self.name = None
        self.path = os.path.abspath(path) if path else None
        self.testsuites = {}

    def init(self, name):
        self.name = name
        if not self.path:
            self.path = os.path.normpath(os.path.join(toplevel, name))
        return self

    def __str__(self):
        return self.name

    def createConfig(self, options):
        return self.Config(options)

    def filterTestSuite(self, testId, config, filters=[], rfilters=[]):
        (pfilters, prfilters) = platform.getFilters(config)
        for includes in [filters, [re.compile(pf) for pf in pfilters]]:
            if len(includes) > 0:
                for f in includes:
                    if f.search(self.name + "/" + testId):
                        break
                else:
                    return True

        for excludes in [rfilters, [re.compile(pf) for pf in prfilters]]:
            if len(excludes) > 0:
                for f in excludes:
                    if f.search(self.name + "/" + testId):
                        return True
        return False

    def loadTestSuites(self, tests, config, filters=[], rfilters=[]):
        global currentMapping
        currentMapping = self
        for test in tests or [""]:
            for root, dirs, files in os.walk(os.path.join(self.getTestsPath(), test.replace('/', os.sep))):

                testId = root[len(self.getTestsPath()) + 1:]
                if os.sep != "/":
                    testId = testId.replace(os.sep, "/")

                if self.filterTestSuite(testId, config, filters, rfilters):
                    continue

                #
                # First check if there's a test.py file in the directory, if there's one use it.
                #
                if "test.py" in files:
                    #
                    # WORKAROUND for Python issue 15230 (fixed in 3.2) where run_path doesn't work correctly.
                    #
                    #runpy.run_path(os.path.join(root, "test.py"))
                    origsyspath = sys.path
                    sys.path = [root] + sys.path
                    runpy.run_module("test", init_globals=globals(), run_name=root)
                    origsyspath = sys.path
                    continue

                #
                # If there's no test.py file in the test directory, we check if there's a common
                # script for the test in scripts/tests. If there's on we use it.
                #
                script = os.path.join(self.getCommonTestsPath(), testId + ".py")
                if os.path.isfile(script):
                    runpy.run_module("tests." + testId.replace("/", "."), init_globals=globals(), run_name=root)
                    continue

                #
                # Finally, we try to "discover/compute" the test by looking up for well-known
                # files.
                #
                testcases = self.computeTestCases(testId, files)
                if testcases:
                    TestSuite(root, testcases)
        currentMapping = None

    def getTestSuites(self, ids=[]):
        if not ids:
            return self.testsuites.values()
        return [self.testsuites[testSuiteId] for testSuiteId in ids if testSuiteId in self.testsuites]

    def addTestSuite(self, testsuite):
        assert len(testsuite.path) > len(self.getTestsPath()) + 1
        testSuiteId = testsuite.path[len(self.getTestsPath()) + 1:].replace('\\', '/')
        self.testsuites[testSuiteId] = testsuite
        return testSuiteId

    def findTestSuite(self, testsuite):
        return self.testsuites.get(testsuite if isinstance(testsuite, str) else testsuite.id)

    def computeTestCases(self, testId, files):

        # Instantiate a new test suite if the directory contains well-known source files.

        def checkFile(f, m):
            try:
                # If given mapping is same as local mapping, just check the files set, otherwise check
                # with the mapping
                return (self.getDefaultSource(f) in files) if m == self else m.hasSource(testId, f)
            except KeyError:
                # Expected if the mapping doesn't support the process type
                return False

        checkClient = lambda f: checkFile(f, self.getClientMapping(testId))
        checkServer = lambda f: checkFile(f, self.getServerMapping(testId))

        testcases = []
        if checkClient("client") and checkServer("server"):
            testcases.append(ClientServerTestCase())
        if checkClient("client") and checkServer("serveramd") and self.getServerMapping(testId) == self:
            testcases.append(ClientAMDServerTestCase())
        if checkClient("client") and len(testcases) == 0:
            testcases.append(ClientTestCase())
        if checkClient("collocated"):
            testcases.append(CollocatedTestCase())
        if len(testcases) > 0:
            return testcases

    def hasSource(self, testId, processType):
        try:
            return os.path.exists(os.path.join(self.getTestsPath(), testId, self.getDefaultSource(processType)))
        except KeyError:
            return False

    def getPath(self):
        return self.path

    def getTestsPath(self):
        return os.path.join(self.path, "test")

    def getCommonTestsPath(self):
        return os.path.join(self.path, "..", "scripts", "tests")

    def getTestCwd(self, process, current):
        return current.testcase.getPath()

    def getDefaultSource(self, processType):
        return processType

    def getDefaultProcesses(self, processType, testsuite):
        #
        # If no server or client is explicitly set with a testcase, getDefaultProcess is called
        # to figure out which process class to instantiate. Based on the processType and the testsuite
        # we instantiate the right default process class.
        #
        if processType is None:
            return []
        elif testsuite.getId().startswith("IceUtil") or testsuite.getId().startswith("Slice"):
            return [SimpleClient()]
        elif testsuite.getId().startswith("IceGrid"):
            if processType in ["client", "collocated"]:
                return [IceGridClient()]
            if processType in ["server", "serveramd"]:
                return [IceGridServer()]
        else:
            return [Server()] if processType in ["server", "serveramd"] else [Client()]

    def getDefaultExe(self, processType, config):
        return processType

    def getClientMapping(self, testId=None):
        # The client mapping is always the same as this mapping.
        return self

    def getServerMapping(self, testId=None):
        # Can be overridden for client-only mapping that relies on another mapping for servers
        return self

    def getBinDir(self, process, current):
        return os.path.join(current.driver.getIceDir(self, current), platform.getBinSubDir(self, process, current))

    def getLibDir(self, process, current):
        return os.path.join(current.driver.getIceDir(self, current), platform.getLibSubDir(self, process, current))

    def getBuildDir(self, name, current):
        return platform.getBuildSubDir(self, name, current)

    def getCommandLine(self, current, process, exe, args):
        cmd = ""
        if process.isFromBinDir():
            # If it's a process from the bin directory, the location is platform specific
            # so we check with the platform.
            cmd = os.path.join(self.getBinDir(process, current), exe)
        elif current.testcase:
            # If it's a process from a testcase, the binary is in the test build directory.
            cmd = os.path.join(current.testcase.getPath(), current.getBuildDir(exe), exe)
        else:
            cmd = exe

        if isinstance(platform, Windows) and not exe.endswith(".exe"):
            cmd += ".exe"

        return cmd + " " + args if args else cmd

    def getProps(self, process, current):
        props = {}
        if isinstance(process, IceProcess):
            if current.config.protocol in ["bt", "bts"]:
                props["Ice.Plugin.IceBT"] = self.getPluginEntryPoint("IceBT", process, current)
            if current.config.protocol in ["ssl", "wss", "bts", "iaps"]:
                props.update(self.getSSLProps(process, current))
        return props

    def getSSLProps(self, process, current):
        sslProps = {
            "Ice.Plugin.IceSSL" : self.getPluginEntryPoint("IceSSL", process, current),
            "IceSSL.Password": "password",
            "IceSSL.DefaultDir": "" if current.config.buildPlatform == "iphoneos" else os.path.join(toplevel, "certs"),
        }

        #
        # If the client doesn't support client certificates, set IceSSL.VerifyPeer to 0
        #
        if isinstance(process, Server):
            if isinstance(current.testsuite.getMapping(), JavaScriptMapping):
                sslProps["IceSSL.VerifyPeer"] = 0

        return sslProps

    def getArgs(self, process, current):
        return []

    def getEnv(self, process, current):
        return {}

    def getOptions(self, current):
        return {}

    def getRunOrder(self):
        return ["Slice", "IceUtil", "Ice", "IceSSL", "IceBox", "Glacier2", "IceGrid", "IceStorm"]

    def getCrossTestSuites(self):
        return [
            "Ice/ami",
            "Ice/info",
            "Ice/exceptions",
            "Ice/enums",
            "Ice/facets",
            "Ice/inheritance",
            "Ice/invoke",
            "Ice/objects",
            "Ice/operations",
            "Ice/proxy",
            "Ice/servantLocator",
            "Ice/slicing/exceptions",
            "Ice/slicing/objects",
            "Ice/optional"
        ]

#
# A Runnable can be used as a "client" for in test cases, it provides
# implements run, setup and teardown methods.
#
class Runnable:

    def __init__(self, desc=None):
        self.desc = desc

    def setup(self, current):
        ### Only called when ran from testcase
        pass

    def teardown(self, current, success):
        ### Only called when ran from testcase
        pass

    def run(self, current):
        pass

#
# A Process describes how to run an executable process.
#
class Process(Runnable):

    processType = None

    def __init__(self, exe=None, outfilters=None, quiet=False, args=None, props=None, envs=None, desc=None,
                 mapping=None, preexec_fn=None, traceProps=None):
        Runnable.__init__(self, desc)
        self.exe = exe
        self.outfilters = outfilters or []
        self.quiet = quiet
        self.args = args or []
        self.props = props or {}
        self.traceProps = traceProps or {}
        self.envs = envs or {}
        self.mapping = mapping
        self.preexec_fn = preexec_fn

    def __str__(self):
        if not self.exe:
            return str(self.__class__)
        return self.exe + (" ({0})".format(self.desc) if self.desc else "")

    def getOutput(self, current, encoding="utf-8"):
        assert(self in current.processes)

        def d(s):
            return s if isPython2 else s.decode(encoding) if isinstance(s, bytes) else s

        output = d(current.processes[self].getOutput())
        try:
            # Apply outfilters to the output
            if len(self.outfilters) > 0:
                lines = output.split('\n')
                newLines = []
                previous = ""
                for line in [line + '\n' for line in lines]:
                    for f in self.outfilters:
                        if isinstance(f, types.LambdaType) or isinstance(f, types.FunctionType):
                            line = f(line)
                        elif f.search(line):
                            break
                    else:
                        if line.endswith('\n'):
                            if previous:
                                newLines.append(previous + line)
                                previous = ""
                            else:
                                newLines.append(line)
                        else:
                            previous += line
                output = "".join(newLines)
            output = output.strip()
            return output + '\n' if output else ""
        except Exception as ex:
            print("unexpected exception while filtering process output:\n" + str(ex))
            raise

    def run(self, current, args=[], props={}, exitstatus=0, timeout=None):
        class WatchDog:

            def __init__(self, timeout):
                self.lastProgressTime = time.time()
                self.lock = threading.Lock()

            def reset(self):
                with self.lock: self.lastProgressTime = time.time()

            def timedOut(self, timeout):
                with self.lock:
                    return (time.time() - self.lastProgressTime) >= timeout

        watchDog = WatchDog(timeout)
        self.start(current, args, props, watchDog=watchDog)
        process = current.processes[self]

        if timeout is None:
            # If it's not a local process use a large timeout as the watch dog might not
            # get invoked (TODO: improve remote processes to use the watch dog)
            timeout = 60 if isinstance(process, Expect.Expect) else 480

        if not self.quiet and not current.driver.isWorkerThread():
            # Print out the process output to stdout if we're running the client form the main thread.
            process.trace(self.outfilters)
        try:
            while True:
                try:
                    process.waitSuccess(exitstatus=exitstatus, timeout=30)
                    break
                except KeyboardInterrupt:
                    current.driver.setInterrupt(True)
                    raise
                except Expect.TIMEOUT:
                    if watchDog and watchDog.timedOut(timeout):
                        print("process {0} is hanging - {1}".format(process, time.strftime("%x %X")))
                        if current.driver.isInterrupted():
                            self.stop(current, False, exitstatus)
                            raise
        finally:
            self.stop(current, True, exitstatus)

    def getEffectiveArgs(self, current, args):
        allArgs = []
        allArgs += current.driver.getArgs(self, current)
        allArgs += current.config.getArgs(self, current)
        allArgs += self.getMapping(current).getArgs(self, current)
        allArgs += current.testcase.getArgs(self, current)
        allArgs += self.getArgs(current)
        allArgs += self.args(self, current) if callable(self.args) else self.args
        allArgs += args
        allArgs = [a.encode("utf-8") if type(a) == "unicode" else str(a) for a in allArgs]
        return allArgs

    def getEffectiveProps(self, current, props):
        allProps = {}
        allProps.update(current.driver.getProps(self, current))
        allProps.update(current.config.getProps(self, current))
        allProps.update(self.getMapping(current).getProps(self, current))
        allProps.update(current.testcase.getProps(self, current))
        allProps.update(self.getProps(current))
        allProps.update(self.props(self, current) if callable(self.props) else self.props)
        allProps.update(props)
        return allProps

    def getEffectiveEnv(self, current):
        allEnvs = {}
        allEnvs.update(self.getMapping(current).getEnv(self, current))
        allEnvs.update(current.testcase.getEnv(self, current))
        allEnvs.update(self.getEnv(current))
        allEnvs.update(self.envs(self, current) if callable(self.envs) else self.envs)
        return allEnvs

    def getEffectiveTraceProps(self, current):
        traceProps = {}
        traceProps.update(current.testcase.getTraceProps(self, current))
        traceProps.update(self.traceProps(self, current) if callable(self.traceProps) else self.traceProps)
        return traceProps

    def start(self, current, args=[], props={}, watchDog=None):
        allArgs = self.getEffectiveArgs(current, args)
        allProps = self.getEffectiveProps(current, props)
        allEnvs = self.getEffectiveEnv(current)

        processController = current.driver.getProcessController(current, self)
        current.processes[self] = processController.start(self, current, allArgs, allProps, allEnvs, watchDog)
        try:
            self.waitForStart(current)
        except:
            self.stop(current)
            raise

    def waitForStart(self, current):
        # To be overridden in specialization to wait for a token indicating the process readiness.
        pass

    def stop(self, current, waitSuccess=False, exitstatus=0):
        if self in current.processes:
            process = current.processes[self]
            try:
                # Wait for the process to exit successfully by itself.
                if not process.isTerminated() and waitSuccess:
                    while True:
                        try:
                            process.waitSuccess(exitstatus=exitstatus, timeout=30)
                            break
                        except KeyboardInterrupt:
                            current.driver.setInterrupt(True)
                            raise
                        except Expect.TIMEOUT:
                            print("process {0} is hanging on shutdown - {1}".format(process, time.strftime("%x %X")))
                            if current.driver.isInterrupted():
                                raise
            finally:
                if not process.isTerminated():
                    process.terminate()
                if not self.quiet: # Write the output to the test case (but not on stdout)
                    current.write(self.getOutput(current), stdout=False)

    def teardown(self, current, success):
        if self in current.processes:
            current.processes[self].teardown(current, success)

    def expect(self, current, pattern, timeout=60):
        assert(self in current.processes and isinstance(current.processes[self], Expect.Expect))
        return current.processes[self].expect(pattern, timeout)

    def sendline(self, current, data):
        assert(self in current.processes and isinstance(current.processes[self], Expect.Expect))
        return current.processes[self].sendline(data)

    def getMatch(self, current):
        assert(self in current.processes and isinstance(current.processes[self], Expect.Expect))
        return current.processes[self].match

    def isStarted(self, current):
        return self in current.processes and not current.processes[self].isTerminated()

    def isFromBinDir(self):
        return False

    def getArgs(self, current):
        return []

    def getProps(self, current):
        return {}

    def getEnv(self, current):
        return {}

    def getMapping(self, current):
        return self.mapping or current.testcase.getMapping()

    def getExe(self, current):
        processType = self.processType or current.testcase.getProcessType(self)
        return self.exe or self.getMapping(current).getDefaultExe(processType, current.config)

    def getCommandLine(self, current, args=""):
        return self.getMapping(current).getCommandLine(current, self, self.getExe(current), args).strip()

#
# A simple client (used to run Slice/IceUtil clients for example)
#
class SimpleClient(Process):
    pass

#
# An IceProcess specialization class. This is used by drivers to figure out if
# the process accepts Ice configuration properties.
#
class IceProcess(Process):
    pass

#
# An Ice server process. It's possible to configure when the server is considered
# ready by setting readyCount or ready. The start method will only return once
# the server is considered "ready". It can also be configure to wait (the default)
# or not wait for shutdown when the stop method is invoked.
#
class Server(IceProcess):

    def __init__(self, exe=None, waitForShutdown=True, readyCount=1, ready=None, startTimeout=120, *args, **kargs):
        IceProcess.__init__(self, exe, *args, **kargs)
        self.waitForShutdown = waitForShutdown
        self.readyCount = readyCount
        self.ready = ready
        self.startTimeout = startTimeout

    def getProps(self, current):
        props = IceProcess.getProps(self, current)
        props.update({
            "Ice.ThreadPool.Server.Size": 1,
            "Ice.ThreadPool.Server.SizeMax": 3,
            "Ice.ThreadPool.Server.SizeWarn": 0,
        })
        props.update(current.driver.getProcessProps(current, self.ready, self.readyCount + (1 if current.config.mx else 0)))
        return props

    def waitForStart(self, current):
        # Wait for the process to be ready
        current.processes[self].waitReady(self.ready, self.readyCount + (1 if current.config.mx else 0), self.startTimeout)

        # Filter out remaining ready messages
        self.outfilters.append(re.compile("[^\n]+ ready"))

        # If we are not asked to be quiet and running from the main thread, print the server output
        if not self.quiet and not current.driver.isWorkerThread():
            current.processes[self].trace(self.outfilters)

    def stop(self, current, waitSuccess=False, exitstatus=0):
        IceProcess.stop(self, current, waitSuccess and self.waitForShutdown, exitstatus)

#
# An Ice client process.
#
class Client(IceProcess):
    pass

#
# Executables for processes inheriting this marker class are looked up in the
# Ice distribution bin directory.
#
class ProcessFromBinDir:

    def isFromBinDir(self):
        return True

class SliceTranslator(ProcessFromBinDir, SimpleClient):

    def __init__(self, translator):
        SimpleClient.__init__(self, exe=translator, quiet=True, mapping=Mapping.getByName("cpp"))

    def getCommandLine(self, current, args=""):
        #
        # Look for slice2py installed by Pip if not found in the bin directory
        #
        if self.exe == "slice2py":
            translator = self.getMapping(current).getCommandLine(current, self, self.getExe(current), "")
            if os.path.exists(translator):
                return translator + " " + args if args else translator
            elif isinstance(platform, Windows):
                return os.path.join(os.path.dirname(sys.executable), "Scripts", "slice2py.exe")
            elif os.path.exists("/usr/local/bin/slice2py"):
                return "/usr/local/bin/slice2py"
            else:
                import slice2py
                return sys.executable + " " + os.path.normpath(
                            os.path.join(slice2py.__file__, "..", "..", "..", "..", "bin", "slice2py"))
        else:
            return Process.getCommandLine(self, current, args)

class EchoServer(Server):

    def __init__(self):
        Server.__init__(self, mapping=Mapping.getByName("cpp"), quiet=True, waitForShutdown=False)

    def getProps(self, current):
        props = Server.getProps(self, current)
        props["Ice.MessageSizeMax"] = 8192 # Don't limit the amount of data to transmit between client/server
        return props

    def getCommandLine(self, current, args=""):
        current.push(self.mapping.findTestSuite("Ice/echo").findTestCase("server"))
        try:
            return Server.getCommandLine(self, current, args)
        finally:
            current.pop()

#
# A test case is composed of servers and clients. When run, all servers are started
# sequentially. When the servers are ready, the clients are also ran sequentially.
# Once all the clients are terminated, the servers are stopped (which waits for the
# successful completion of the server).
#
# A TestCase is also a "Runnable", like the Process class. In other words, it can be
# used a client to allow nested test cases.
#
class TestCase(Runnable):

    def __init__(self, name, client=None, clients=None, server=None, servers=None, args=None, props=None, envs=None,
                 options=None, desc=None, traceProps=None):
        Runnable.__init__(self, desc)

        self.name = name
        self.parent = None
        self.mapping = None
        self.testsuite = None
        self.options = options or {}
        self.args = args or []
        self.props = props or {}
        self.traceProps = traceProps or {}
        self.envs = envs or {}

        #
        # Setup client list, "client" can be a string in which case it's assumed to
        # to the client executable name.
        #
        self.clients = clients
        if client:
            client = Client(exe=client) if isinstance(client, str) else client
            self.clients = [client] if not self.clients else self.clients + [client]

        #
        # Setup server list, "server" can be a string in which case it's assumed to
        # to the server executable name.
        #
        self.servers = servers
        if server:
            server = Server(exe=server) if isinstance(server, str) else server
            self.servers = [server] if not self.servers else self.servers + [server]

    def __str__(self):
        return self.name

    def init(self, mapping, testsuite):
        # init is called when the testcase is added to the given testsuite
        self.mapping = mapping
        self.testsuite = testsuite

        #
        # If no clients are explicitly specified, we instantiate one if getClientType()
        # returns the type of client to instantiate (client, collocated, etc)
        #
        if not self.clients:
            self.clients = self.mapping.getDefaultProcesses(self.getClientType(), testsuite)

        #
        # If no servers are explicitly specified, we instantiate one if getServerType()
        # returns the type of server to instantiate (server, serveramd, etc)
        #
        if not self.servers:
            self.servers = self.mapping.getDefaultProcesses(self.getServerType(), testsuite)

    def getOptions(self, current):
        return self.options(current) if callable(self.options) else self.options

    def canRun(self, current):
        # Can be overriden
        return True

    def setupServerSide(self, current):
        # Can be overridden to perform setup activities before the server side is started
        pass

    def teardownServerSide(self, current, success):
        # Can be overridden to perform terddown after the server side is stopped
        pass

    def setupClientSide(self, current):
        # Can be overridden to perform setup activities before the client side is started
        pass

    def teardownClientSide(self, current, success):
        # Can be overridden to perform terddown after the client side is stopped
        pass

    def startServerSide(self, current):
        for server in self.servers:
            self._startServer(current, server)

    def stopServerSide(self, current, success):
        for server in reversed(self.servers):
            self._stopServer(current, server, success)

    def runClientSide(self, current):
        for client in self.clients:
            self._runClient(current, client)

    def getTestSuite(self):
        return self.testsuite

    def getParent(self):
        return self.parent

    def getName(self):
        return self.name

    def getPath(self):
        return self.testsuite.getPath()

    def getMapping(self):
        return self.mapping

    def getArgs(self, process, current):
        return self.args

    def getProps(self, process, current):
        return self.props

    def getTraceProps(self, process, current):
        return self.traceProps

    def getEnv(self, process, current):
        return self.envs

    def getProcessType(self, process):
        if process in self.clients:
            return self.getClientType()
        elif process in self.servers:
            return self.getServerType()
        elif isinstance(process, Server):
            return self.getServerType()
        else:
            return self.getClientType()

    def getClientType(self):
        # Overridden by test case specialization to specify the type of client to instantiate
        # if no client is explicitly provided
        return None

    def getServerType(self):
        # Overridden by test case specialization to specify the type of client to instantiate
        # if no server is explicitly provided
        return None

    def getServerTestCase(self, cross=None):
        testsuite = (cross or self.mapping).getServerMapping(self.testsuite.getId()).findTestSuite(self.testsuite)
        return testsuite.findTestCase(self) if testsuite else None

    def getClientTestCase(self):
        testsuite = self.mapping.getClientMapping(self.testsuite.getId()).findTestSuite(self.testsuite)
        return testsuite.findTestCase(self) if testsuite else None

    def _startServerSide(self, current):
        # Set the host to use for the server side
        current.push(self)
        current.host = current.driver.getProcessController(current).getHost(current)
        self.setupServerSide(current)
        try:
            self.startServerSide(current)
            return current.host
        except:
            self._stopServerSide(current, False)
            raise
        finally:
            current.pop()

    def _stopServerSide(self, current, success):
        current.push(self)
        try:
            self.stopServerSide(current, success)
        finally:
            for server in reversed(self.servers):
                if server.isStarted(current):
                    self._stopServer(current, server, False)
            self.teardownServerSide(current, success)
            current.pop()

    def _startServer(self, current, server):
        if server.desc:
            current.write("starting {0}... ".format(server.desc))
        server.setup(current)
        server.start(current)
        if server.desc:
            current.writeln("ok")

    def _stopServer(self, current, server, success):
        try:
            server.stop(current, success)
        except:
            success = False
            raise
        finally:
            server.teardown(current, success)

    def _runClientSide(self, current, host=None):
        current.push(self, host)
        self.setupClientSide(current)
        success = False
        try:
            self.runClientSide(current)
            success = True
        finally:
            self.teardownClientSide(current, success)
            current.pop()

    def _runClient(self, current, client):
        success = False
        if client.desc:
            current.writeln("running {0}...".format(client.desc))
        client.setup(current)
        try:
            client.run(current)
            success = True
        finally:
            client.teardown(current, success)

    def run(self, current):
        try:
            current.push(self)
            if not self.parent:
                current.result.started(current)
            self.setup(current)
            self.runWithDriver(current)
            self.teardown(current, True)
            if not self.parent:
                current.result.succeeded(current)
        except Exception as ex:
            self.teardown(current, False)
            if not self.parent:
                current.result.failed(current, traceback.format_exc() if current.driver.debug else str(ex))
            raise
        finally:
            current.pop()

class ClientTestCase(TestCase):

    def __init__(self, name="client", *args, **kargs):
        TestCase.__init__(self, name, *args, **kargs)

    def runWithDriver(self, current):
        current.driver.runTestCase(current)

    def getClientType(self):
        return "client"

class ClientServerTestCase(ClientTestCase):

    def __init__(self, name="client/server", *args, **kargs):
        TestCase.__init__(self, name, *args, **kargs)

    def runWithDriver(self, current):
        current.driver.runClientServerTestCase(current)

    def getServerType(self):
        return "server"

class CollocatedTestCase(ClientTestCase):

    def __init__(self, name="collocated", *args, **kargs):
        TestCase.__init__(self, name, *args, **kargs)

    def getClientType(self):
        return "collocated"

class ClientAMDServerTestCase(ClientServerTestCase):

    def __init__(self, name="client/amd server", *args, **kargs):
        ClientServerTestCase.__init__(self, name, *args, **kargs)

    def getServerType(self):
        return "serveramd"

class Result:

    getKey = lambda self, current: (current.testcase, current.config) if isinstance(current, Driver.Current) else current
    getDesc = lambda self, current: current.desc if isinstance(current, Driver.Current) else ""

    def __init__(self, testsuite, writeToStdout):
        self.testsuite = testsuite
        self._failed = {}
        self._skipped = {}
        self._stdout = StringIO()
        self._writeToStdout = writeToStdout
        self._testcases = {}
        self._duration = 0
        self._testCaseDuration = 0;

    def start(self):
        self._duration = time.time()

    def finished(self):
        self._duration = time.time() - self._duration

    def started(self, current):
        self._testCaseDuration = time.time();
        self._start = self._stdout.tell()

    def failed(self, current, exception):
        print(exception)
        key = self.getKey(current)
        self._testCaseDuration = time.time() - self._testCaseDuration;
        self.writeln("\ntest in {0} failed:\n{1}".format(self.testsuite, exception))
        self._testcases[key] = (self._start, self._stdout.tell(), self._testCaseDuration, self.getDesc(current))
        self._failed[key] = exception

        # If ADDRINUSE, dump the current processes
        output = self.getOutput(key)
        for s in ["EADDRINUSE", "Address already in use"]:
            if output.find(s) >= 0:
                if isinstance(platform, Windows):
                    self.writeln(run("netstat -on"))
                    self.writeln(run("powershell.exe \"Get-Process | Select id,name,path\""))
                else:
                    self.writeln(run("lsof -n -P -i; ps ax"))

    def succeeded(self, current):
        key = self.getKey(current)
        self._testCaseDuration = time.time() - self._testCaseDuration;
        self._testcases[key] = (self._start, self._stdout.tell(), self._testCaseDuration, self.getDesc(current))

    def skipped(self, current, reason):
        self.writeln("skipped, " + reason)
        self._skipped[self.getKey(current)] = reason

    def isSuccess(self):
        return len(self._failed) == 0

    def getFailed(self):
        return self._failed

    def getDuration(self):
        return self._duration

    def getOutput(self, key=None):
        if key:
            if key in self._testcases:
                (start, end, duration, desc) = self._testcases[key]
                self._stdout.seek(start)
                try:
                    return self._stdout.read(end - start)
                finally:
                    self._stdout.seek(0, os.SEEK_END)

        return self._stdout.getvalue()

    def write(self, msg, stdout=True):
        if self._writeToStdout and stdout:
            try:
                sys.stdout.write(msg)
            except UnicodeEncodeError:
                #
                # The console doesn't support the encoding of the message, we convert the message
                # to an UTF-8 byte sequence and print out the byte sequence. We replace all the
                # double backslash from the byte sequence string representation to single back
                # slash.
                #
                sys.stdout.write(str(msg.encode("utf-8")).replace("\\\\", "\\"))
            sys.stdout.flush()
        self._stdout.write(msg)

    def writeln(self, msg, stdout=True):
        if self._writeToStdout and stdout:
            try:
                print(msg)
            except UnicodeEncodeError:
                #
                # The console doesn't support the encoding of the message, we convert the message
                # to an UTF-8 byte sequence and print out the byte sequence. We replace all the
                # double backslash from the byte sequence string representation to single back
                # slash.
                #
                print(str(msg.encode("utf-8")).replace("\\\\", "\\"))
        self._stdout.write(msg)
        self._stdout.write("\n")

    def writeAsXml(self, out, hostname=""):
        out.write('  <testsuite tests="{0}" failures="{1}" skipped="{2}" time="{3:.9f}" name="{5}/{4}">\n'
                    .format(len(self._testcases) - 2,
                            len(self._failed),
                            0,
                            self._duration,
                            self.testsuite,
                            self.testsuite.getMapping()))

        for (k, v) in self._testcases.items():
            if isinstance(k, str):
                # Don't keep track of setup/teardown steps
                continue

            # Don't write skipped tests, this doesn't really provide useful information and clutters
            # the output.
            if k in self._skipped:
                continue

            (tc, cf) = k
            (s, e, d, c) = v
            if c:
                name = "{0} [{1}]".format(tc, c)
            else:
                name = str(tc)
            if hostname:
                name += " on " + hostname
            out.write('    <testcase name="{0}" time="{1:.9f}" classname="{2}.{3}">\n'
                      .format(name,
                              d,
                              self.testsuite.getMapping(),
                              self.testsuite.getId().replace("/", ".")))
            if k in self._failed:
                last = self._failed[k].strip().split('\n')
                if len(last) > 0:
                    last = last[len(last) - 1]
                if hostname:
                    last = "Failed on {0}\n{1}".format(hostname, last)
                out.write('      <failure message={1}>{0}</failure>\n'.format(escapeXml(self._failed[k]),
                                                                              escapeXml(last, True)))
            # elif k in self._skipped:
            #     out.write('      <skipped message="{0}"/>\n'.format(escapeXml(self._skipped[k], True)))
            out.write('      <system-out>\n')
            if hostname:
                out.write('Running on {0}\n'.format(hostname))
            out.write(escapeXml(self.getOutput(k)))
            out.write('      </system-out>\n')
            out.write('    </testcase>\n')

        out.write(  '</testsuite>\n')

class TestSuite:

    def __init__(self, path, testcases=None, options=None, libDirs=None, runOnMainThread=False, chdir=False,
                 multihost=True, mapping=None):
        global currentMapping
        self.path = os.path.dirname(path) if os.path.basename(path) == "test.py" else path
        self.mapping = currentMapping or Mapping.getByPath(self.path)
        self.id = self.mapping.addTestSuite(self)
        self.options = options or {}
        self.libDirs = libDirs or []
        self.runOnMainThread = runOnMainThread
        self.chdir = chdir
        self.multihost = multihost
        if self.chdir:
            # Only tests running on main thread can change the current working directory
            self.runOnMainThread = True

        if testcases is None:
            files = [f for f in os.listdir(self.path) if os.path.isfile(os.path.join(self.path, f))]
            testcases = self.mapping.computeTestCases(self.id, files)
        self.testcases = OrderedDict()
        for testcase in testcases if testcases else []:
            testcase.init(self.mapping, self)
            if testcase.name in self.testcases:
                raise RuntimeError("duplicate testcase {0} in testsuite {1}".format(testcase, self))
            self.testcases[testcase.name] = testcase

    def __str__(self):
        return self.id

    def getId(self):
        return self.id

    def getOptions(self, current):
        return self.options(current) if callable(self.options) else self.options

    def getPath(self):
        return self.path

    def getMapping(self):
        return self.mapping

    def getLibDirs(self):
        return self.libDirs

    def isMainThreadOnly(self, driver):
        for m in [CppMapping, JavaMapping, CSharpMapping]:
            config = driver.configs[self.mapping]
            # TODO: WORKAROUND for ICE-8175
            if self.id.startswith("IceStorm"):
                return True
            elif isinstance(self.mapping, AndroidMapping) or isinstance(self.mapping, AndroidCompatMapping):
                return True
            elif isinstance(self.mapping, m):
                if "iphone" in config.buildPlatform or config.uwp:
                    return True # Not supported yet for tests that require a remote process controller
                return self.runOnMainThread
        else:
            return True

    def addTestCase(self, testcase):
        if testcase.name in self.testcases:
            raise RuntimeError("duplicate testcase {0} in testsuite {1}".format(testcase, self))
        testcase.init(self.mapping, self)
        self.testcases[testcase.name] = testcase

    def findTestCase(self, testcase):
        return self.testcases.get(testcase if isinstance(testcase, str) else testcase.name)

    def getTestCases(self):
        return self.testcases.values()

    def setup(self, current):
        pass

    def run(self, current):
        try:
            current.result.start()
            cwd=None
            if self.chdir:
                cwd = os.getcwd()
                os.chdir(self.path)
            current.driver.runTestSuite(current)
        finally:
            if cwd: os.chdir(cwd)
            current.result.finished()

    def teardown(self, current, success):
        pass

    def isMultiHost(self):
        return self.multihost

    def isCross(self):
        # Only run the tests that support cross testing --all-cross or --cross
        return self.id in self.mapping.getCrossTestSuites()

class ProcessController:

    def __init__(self, current):
        pass

    def start(self, process, current, args, props, envs, watchDog):
        raise NotImplemented()

    def destroy(self, driver):
        pass

class LocalProcessController(ProcessController):

    class LocalProcess(Expect.Expect):

        def __init__(self, traceFile, *args, **kargs):
            Expect.Expect.__init__(self, *args, **kargs)
            self.traceFile = traceFile

        def waitReady(self, ready, readyCount, startTimeout):
            if ready:
                self.expect("%s ready\n" % ready, timeout = startTimeout)
            else:
                while readyCount > 0:
                    self.expect("[^\n]+ ready\n", timeout = startTimeout)
                    readyCount -= 1

        def isTerminated(self):
            return self.p is None

        def teardown(self, current, success):
            if self.traceFile:
                if success or current.driver.isInterrupted():
                    os.remove(self.traceFile)
                else:
                    current.writeln("saved {0}".format(self.traceFile))

    def getHost(self, current):
        # Depending on the configuration, either use an IPv4, IPv6 or BT address for Ice.Default.Host
        if current.config.protocol == "bt":
            if not current.driver.hostBT:
                raise Test.Common.TestCaseFailedException("no Bluetooth address set with --host-bt")
            return current.driver.hostBT
        elif current.config.ipv6:
            return current.driver.hostIPv6 or "::1"
        else:
            return current.driver.host or "127.0.0.1"

    def start(self, process, current, args, props, envs, watchDog):

        #
        # Props and arguments can use the format parameters set below in the kargs
        # dictionary. It's time to convert them to their values.
        #
        kargs = {
            "process": process,
            "testcase": current.testcase,
            "testdir": current.testsuite.getPath(),
            "builddir": current.getBuildDir(process.getExe(current)),
            "icedir" : current.driver.getIceDir(current.testcase.getMapping(), current),
            "iceboxconfigext": "" if not current.config.netframework else ".{0}".format(current.config.netframework)
        }

        traceFile = ""
        if not isinstance(process.getMapping(current), JavaScriptMapping):
            traceProps = process.getEffectiveTraceProps(current)
            if traceProps:
                if "Ice.ProgramName" in props:
                    programName = props["Ice.ProgramName"]
                else:
                    programName = process.exe or current.testcase.getProcessType(process)
                traceFile = os.path.join(current.testsuite.getPath(),
                                         "{0}-{1}.log".format(programName, time.strftime("%m%d%y-%H%M")))
                if isinstance(process.getMapping(current), ObjCMapping):
                    traceProps["Ice.StdErr"] = traceFile
                else:
                    traceProps["Ice.LogFile"] = traceFile
            props.update(traceProps)

        args = ["--{0}={1}".format(k, val(v)) for k,v in props.items()] + [val(a) for a in args]
        for k, v in envs.items():
            envs[k] = val(v, quoteValue=False)

        cmd = ""
        if current.driver.valgrind:
            cmd += "valgrind -q --child-silent-after-fork=yes --leak-check=full --suppressions=\"{0}\" ".format(
                                                                os.path.join(toplevel, "config", "valgrind.sup"))
        exe = process.getCommandLine(current, " ".join(args))
        cmd += exe.format(**kargs)

        if current.driver.debug:
            if len(envs) > 0:
                current.writeln("({0} env={1})".format(cmd, envs))
            else:
                current.writeln("({0})".format(cmd))

        env = os.environ.copy()
        env.update(envs)
        mapping = process.getMapping(current)
        cwd = mapping.getTestCwd(process, current)
        process = LocalProcessController.LocalProcess(command=cmd,
                                                      startReader=False,
                                                      env=env,
                                                      cwd=cwd,
                                                      desc=process.desc or exe,
                                                      preexec_fn=process.preexec_fn,
                                                      mapping=str(mapping),
                                                      traceFile=traceFile)
        process.startReader(watchDog)
        return process

class RemoteProcessController(ProcessController):

    class RemoteProcess:
        def __init__(self, exe, proxy):
            self.exe = exe
            self.proxy = proxy
            self.terminated = False
            self.stdout = False

        def __str__(self):
            return "{0} proxy={1}".format(self.exe, self.proxy)

        def waitReady(self, ready, readyCount, startTimeout):
            self.proxy.waitReady(startTimeout)

        def waitSuccess(self, exitstatus=0, timeout=60):
            import Ice
            try:
                result = self.proxy.waitSuccess(timeout)
            except Ice.UserException:
                raise Expect.TIMEOUT("waitSuccess timeout")
            except Ice.LocalException:
                raise
            if exitstatus != result:
                raise RuntimeError("unexpected exit status: expected: %d, got %d\n" % (exitstatus, result))

        def getOutput(self):
            return self.output

        def trace(self, outfilters):
            self.stdout = True

        def isTerminated(self):
            return self.terminated

        def terminate(self):
            self.output = self.proxy.terminate().strip()
            self.terminated = True
            if self.stdout and self.output:
                print(self.output)

        def teardown(self, current, success):
            pass

    def __init__(self, current, endpoints=None):
        self.processControllerProxies = {}
        self.controllerApps = []
        self.cond = threading.Condition()
        if endpoints:
            comm = current.driver.getCommunicator()
            import Test

            class ProcessControllerRegistryI(Test.Common.ProcessControllerRegistry):

                def __init__(self, remoteProcessController):
                    self.remoteProcessController = remoteProcessController

                def setProcessController(self, proxy, current):
                    import Test
                    proxy = Test.Common.ProcessControllerPrx.uncheckedCast(current.con.createProxy(proxy.ice_getIdentity()))
                    self.remoteProcessController.setProcessController(proxy)

            self.adapter = comm.createObjectAdapterWithEndpoints("Adapter", endpoints)
            self.adapter.add(ProcessControllerRegistryI(self), comm.stringToIdentity("Util/ProcessControllerRegistry"))
            self.adapter.activate()
        else:
            self.adapter = None

    def __str__(self):
        return "remote controller"

    def getHost(self, current):
        return self.getController(current).getHost(current.config.protocol, current.config.ipv6)

    def getController(self, current):
        ident = self.getControllerIdentity(current)
        if type(ident) == str:
            ident = current.driver.getCommunicator().stringToIdentity(ident)

        import Ice
        import Test

        proxy = None
        with self.cond:
            if ident in self.processControllerProxies:
                proxy = self.processControllerProxies[ident]
        if proxy:
            try:
                proxy.ice_ping()
                return proxy
            except Ice.NoEndpointException:
                self.clearProcessController(proxy)

        comm = current.driver.getCommunicator()

        if current.driver.controllerApp:
            if ident in self.controllerApps:
                self.restartControllerApp(current, ident) # Controller must have crashed, restart it
            else:
                self.controllerApps.append(ident)
                self.startControllerApp(current, ident)

        if not self.adapter:
            # Use well-known proxy and IceDiscovery to discover the process controller object from the app.
            proxy = Test.Common.ProcessControllerPrx.uncheckedCast(comm.stringToProxy(comm.identityToString(ident)))
            try:
                proxy.ice_ping()
            except Exception as ex:
                raise RuntimeError("couldn't reach the remote controller `{0}'".format(proxy))

            with self.cond:
                self.processControllerProxies[ident] = proxy
                return self.processControllerProxies[ident]
        else:
            # Wait 10 seconds for a process controller to be registered with the ProcessControllerRegistry
            with self.cond:
                if not ident in self.processControllerProxies:
                    self.cond.wait(10)
                if ident in self.processControllerProxies:
                    return self.processControllerProxies[ident]
            raise RuntimeError("couldn't reach the remote controller `{0}'".format(ident))

    def setProcessController(self, proxy):
        with self.cond:
            self.processControllerProxies[proxy.ice_getIdentity()] = proxy
            conn = proxy.ice_getConnection()
            if(hasattr(conn, "setCloseCallback")):
                proxy.ice_getConnection().setCloseCallback(lambda conn : self.clearProcessController(proxy, conn))
            else:
                import Ice
                class CallbackI(Ice.ConnectionCallback):
                    def __init__(self, registry):
                        self.registry = registry

                    def heartbeath(self, conn):
                        pass

                    def closed(self, conn):
                        self.registry.clearProcessController(proxy, conn)

                proxy.ice_getConnection().setCallback(CallbackI(self))

            self.cond.notifyAll()

    def clearProcessController(self, proxy, conn=None):
        with self.cond:
            if proxy.ice_getIdentity() in self.processControllerProxies:
                if not conn:
                    conn = proxy.ice_getCachedConnection()
                if conn == self.processControllerProxies[proxy.ice_getIdentity()].ice_getCachedConnection():
                    del self.processControllerProxies[proxy.ice_getIdentity()]

    def startControllerApp(self, current, ident):
        pass

    def restartControllerApp(self, current, ident):
        self.stopControllerApp(ident)
        self.startControllerApp(current, ident)

    def stopControllerApp(self, ident):
        pass

    def start(self, process, current, args, props, envs, watchDog):
        # Get the process controller
        processController = self.getController(current)

        # TODO: support envs?

        exe = process.getExe(current)
        args = ["--{0}={1}".format(k, val(v, quoteValue=False)) for k,v in props.items()] + [val(a) for a in args]
        if current.driver.debug:
            current.writeln("(executing `{0}/{1}' on `{2}' args = {3})".format(current.testsuite, exe, self, args))
        prx = processController.start(str(current.testsuite), exe, args)

        # Create bi-dir proxy in case we're talking to a bi-bir process controller.
        if self.adapter:
            prx = processController.ice_getConnection().createProxy(prx.ice_getIdentity())
        import Test
        return RemoteProcessController.RemoteProcess(exe, Test.Common.ProcessPrx.uncheckedCast(prx))

    def destroy(self, driver):
        if driver.controllerApp:
            for ident in self.controllerApps:
                self.stopControllerApp(ident)
            self.controllerApps = []
        if self.adapter:
            self.adapter.destroy()

class AndroidProcessController(RemoteProcessController):

    def __init__(self, current):
        run("adb kill-server")
        RemoteProcessController.__init__(self, current, "tcp -h 127.0.0.1 -p 15001" if current.config.androidemulator else None)
        self.device = current.config.device
        self.avd = current.config.avd
        self.emulator = None # Keep a reference to the android emulator process

    def __str__(self):
        return "Android"

    def getControllerIdentity(self, current):
        if isinstance(current.testcase.getMapping(), AndroidMapping):
            return "Android/ProcessController"
        else:
            return "AndroidCompat/ProcessController"

    def adb(self):
        return "adb -s {}".format(self.device) if self.device else "adb"

    def startEmulator(self, avd):
        #
        # First check if the AVD image is available
        #
        print("starting the emulator... ")
        out = run("emulator -list-avds")
        if avd not in out:
            raise RuntimeError("couldn't find AVD `{}'".format(avd))

        #
        # Find and unused port to run android emulator, between 5554 and 5584
        #
        port = -1
        out = run("adb devices -l")
        for p in range(5554, 5586, 2):
            if not "emulator-{}".format(p) in out:
                port = p

        if port == -1:
            raise RuntimeError("cannot find free port in range 5554-5584, to run android emulator")

        self.device = "emulator-{}".format(port)
        cmd = "emulator -avd {0} -port {1} -noaudio -no-window -no-snapshot".format(avd, port)
        self.emulator = subprocess.Popen(cmd, shell=True)

        if self.emulator.poll():
            raise RuntimeError("failed to start the Android emulator `{}' on port {}".format(avd, port))

        self.avd = avd

        #
        # Wait for the device to be ready
        #
        t = time.time()
        while True:
            try:
                lines = run("{} shell getprop sys.boot_completed".format(self.adb()))
                if len(lines) > 0 and lines[0].strip() == "1":
                    break
            except RuntimeError:
                pass # expected if device is offline
            #
            # If the emulator doesn't complete boot in 300 seconds give up
            #
            if (time.time() - t) > 300:
                raise RuntimeError("couldn't start the Android emulator `{}'".format(avd))
            time.sleep(2)
        print(" ok")

    def startControllerApp(self, current, ident):

        # Stop previous controller app before starting new one
        for ident in self.controllerApps:
            self.stopControllerApp(ident)

        if current.config.avd:
            self.startEmulator(current.config.avd)
        elif current.config.androidemulator:
            # Create Android Virtual Device
            sdk = current.testcase.getMapping().getSDKPackage()
            try:
                run("avdmanager delete avd -n IceTests") # Delete the created device
            except:
                pass
            run("sdkmanager \"{0}\"".format(sdk))
            run("avdmanager create avd -k \"{0}\" -d \"Nexus 6\" -n IceTests".format(sdk))
            self.startEmulator("IceTests")
        elif not self.device:
            raise RuntimeError("no Android device specified to run the controller application")

        apk = os.path.join(current.testcase.getMapping().getPath(),
                           "controller/build/outputs/apk/debug/testController-debug.apk")
        run("{} install -t -r {}".format(self.adb(), apk))
        run("{} shell am start -n com.zeroc.testcontroller/.ControllerActivity".format(self.adb()))

    def stopControllerApp(self, ident):
        try:
            run("{} shell pm uninstall com.zeroc.testcontroller".format(self.adb()))
        except:
            pass

        if self.avd:
            try:
                run("{} emu kill".format(self.adb()))
            except:
                pass

            try:
                run("adb kill-server")
            except:
                pass

            if self.avd == "IceTests":
                try:
                    run("avdmanager delete avd -n IceTests") # Delete the created device
                except:
                    pass

        #
        # Wait for the emulator to shutdown
        #
        if self.emulator:
            sys.stdout.write("Waiting for the emulator to shutdown..")
            sys.stdout.flush()
            while True:
                if self.emulator.poll() != None:
                    print(" ok")
                    break
                sys.stdout.write(".")
                sys.stdout.flush()
                time.sleep(0.5)

class iOSSimulatorProcessController(RemoteProcessController):

    device = "iOSSimulatorProcessController"
    deviceID = "com.apple.CoreSimulator.SimDeviceType.iPhone-6"
    appPath = "ios/controller/build"

    def __init__(self, current):
        RemoteProcessController.__init__(self, current)
        self.simulatorID = None
        self.runtimeID = None
        # Pick the last iOS simulator runtime ID in the list of iOS simulators (assumed to be the latest).
        for r in run("xcrun simctl list runtimes").split('\n'):
            m = re.search("iOS .* \(.*\) - (.*)", r)
            if m:
                self.runtimeID = m.group(1)
        if not self.runtimeID:
            self.runtimeID = "com.apple.CoreSimulator.SimRuntime.iOS-11-0" # Default value

    def __str__(self):
        return "iOS Simulator"

    def getControllerIdentity(self, current):
        if isinstance(current.testcase.getMapping(), ObjCMapping):
            if current.config.arc:
                return "iPhoneSimulator/com.zeroc.ObjC-ARC-Test-Controller"
            else:
                return "iPhoneSimulator/com.zeroc.ObjC-Test-Controller"
        elif isinstance(current.testcase.getMapping(), CppMapping):
            if current.config.cpp11:
                return "iPhoneSimulator/com.zeroc.Cpp11-Test-Controller"
            else:
                return "iPhoneSimulator/com.zeroc.Cpp98-Test-Controller"
        else:
            raise RuntimeError("can't run tests from the `{0}' mapping on iOS".format(current.testcase.getMapping()))

    def startControllerApp(self, current, ident):
        mapping = current.testcase.getMapping()
        if isinstance(mapping, ObjCMapping):
            appName = "Objective-C ARC Test Controller.app" if current.config.arc else "Objective-C Test Controller.app"
        else:
            assert(isinstance(mapping, CppMapping))
            appName = "C++11 Test Controller.app" if current.config.cpp11 else "C++98 Test Controller.app"

        sys.stdout.write("launching simulator... ")
        sys.stdout.flush()
        try:
            run("xcrun simctl boot \"{0}\"".format(self.device))
        except Exception as ex:
            if str(ex).find("Booted") >= 0:
                pass
            elif str(ex).find("Invalid device") >= 0:
                # Create the simulator device if it doesn't exist
                self.simulatorID = run("xcrun simctl create \"{0}\" {1} {2}".format(self.device, self.deviceID, self.runtimeID))
                run("xcrun simctl boot \"{0}\"".format(self.device))
            else:
                raise
        print("ok")

        sys.stdout.write("launching {0}... ".format(appName))
        sys.stdout.flush()
        path = os.path.join(mapping.getTestsPath(), self.appPath, "Debug-iphonesimulator", appName)
        if not os.path.exists(path):
            path = os.path.join(mapping.getTestsPath(), self.appPath, "Release-iphonesimulator", appName)
        if not os.path.exists(path):
            raise RuntimeError("couldn't find iOS simulator controller application, did you build it?")
        run("xcrun simctl install \"{0}\" \"{1}\"".format(self.device, path))
        run("xcrun simctl launch \"{0}\" {1}".format(self.device, ident.name))
        print("ok")

    def restartControllerApp(self, current, ident):
        try:
            run("xcrun simctl terminate \"{0}\" {1}".format(self.device, ident.name))
        except:
            pass
        run("xcrun simctl launch \"{0}\" {1}".format(self.device, ident.name))

    def stopControllerApp(self, ident):
        try:
            run("xcrun simctl uninstall \"{0}\" {1}".format(self.device, ident.name))
        except:
            pass

    def destroy(self, driver):
        RemoteProcessController.destroy(self, driver)

        sys.stdout.write("shutting down simulator... ")
        sys.stdout.flush()
        try:
            run("xcrun simctl shutdown \"{0}\"".format(self.simulatorID))
        except:
            pass
        print("ok")

        if self.simulatorID:
            sys.stdout.write("destroying simulator... ")
            sys.stdout.flush()
            try:
                run("xcrun simctl delete \"{0}\"".format(self.simulatorID))
            except:
                pass
            print("ok")

class iOSDeviceProcessController(RemoteProcessController):

    appPath = "cpp/test/ios/controller/build"

    def __init__(self, current):
        RemoteProcessController.__init__(self, current)

    def __str__(self):
        return "iOS Device"

    def getControllerIdentity(self, current):
        if isinstance(current.testcase.getMapping(), ObjCMapping):
            if current.config.arc:
                return "iPhoneOS/com.zeroc.ObjC-ARC-Test-Controller"
            else:
                return "iPhoneOS/com.zeroc.ObjC-Test-Controller"
        else:
            assert(isinstance(current.testcase.getMapping(), CppMapping))
            if current.config.cpp11:
                return "iPhoneOS/com.zeroc.Cpp11-Test-Controller"
            else:
                return "iPhoneOS/com.zeroc.Cpp98-Test-Controller"

    def startControllerApp(self, current, ident):
        # TODO: use ios-deploy to deploy and run the application on an attached device?
        pass

    def stopControllerApp(self, ident):
        pass

class UWPProcessController(RemoteProcessController):

    def __init__(self, current):
        RemoteProcessController.__init__(self, current, "tcp -h 127.0.0.1 -p 15001")
        self.name = "ice-uwp-controller"
        self.appUserModelId = "ice-uwp-controller_3qjctahehqazm"

    def __str__(self):
        return "UWP"

    def getControllerIdentity(self, current):
        return "UWP/ProcessController"

    def startControllerApp(self, current, ident):
        platform = current.config.buildPlatform
        config = current.config.buildConfig
        layout = os.path.join(toplevel, "cpp", "test", platform, config, "AppX")

        arch = "x86" if platform == "Win32" else platform
        self.packageFullName = "{0}_1.0.0.0_{1}__3qjctahehqazm".format(self.name, arch)

        prefix = "controller_1.0.0.0_{0}{1}".format(platform, "_{0}".format(config) if config == "Debug" else "")
        package = os.path.join(toplevel, "cpp", "msbuild", "AppPackages", "controller",
            "{0}_Test".format(prefix), "{0}.appx".format(prefix))

        #
        # If the application is already installed remove it, this will also take care
        # of closing it.
        #
        if self.name in run("powershell Get-AppxPackage -Name {0}".format(self.name)):
            run("powershell Remove-AppxPackage {0}".format(self.packageFullName))

        #
        # Remove any previous package we have extracted to ensure we use a
        # fresh build
        #
        if os.path.exists(layout):
            shutil.rmtree(layout)
        os.makedirs(layout)

        print("Unpacking package: {0} to {1}....".format(os.path.basename(package), layout))
        run("MakeAppx.exe unpack /p \"{0}\" /d \"{1}\" /l".format(package, layout))

        print("Registering application to run from layout...")
        vclibs = "Microsoft.VCLibs.140.00.Debug" if config == "Debug" else "Microsoft.VCLibs.140.00"
        if vclibs not in run("powershell Get-AppxPackage -Name {0}".format(vclibs)):
            dependenciesDir = os.path.join(os.path.dirname(package), "Dependencies", arch)
            run("powershell Add-AppxPackage -Path \"{0}\" -ForceApplicationShutdown".format(
                os.path.join(dependenciesDir, "Microsoft.VCLibs.{0}.14.00.appx".format(arch))))

        run("powershell Add-AppxPackage -Register \"{0}/AppxManifest.xml\" -ForceApplicationShutdown".format(layout))
        run("CheckNetIsolation LoopbackExempt -a -n={0}".format(self.appUserModelId))

        #
        # microsoft.windows.softwarelogo.appxlauncher.exe returns the PID as return code
        # and 0 on case of failures. We pass err=True to run to handle this.
        #
        print("starting UWP controller app...")
        run('"{0}" {1}!App'.format(
            "C:/Program Files (x86)/Windows Kits/10/App Certification Kit/microsoft.windows.softwarelogo.appxlauncher.exe",
            self.appUserModelId), err=True)

    def stopControllerApp(self, ident):
        try:
            run("powershell Remove-AppxPackage {0}".format(self.packageFullName))
            run("CheckNetIsolation LoopbackExempt -c -n={0}".format(self.appUserModelId))
        except:
            pass

class BrowserProcessController(RemoteProcessController):

    def __init__(self, current):
        self.host = current.driver.host or "127.0.0.1"
        RemoteProcessController.__init__(self, current, "ws -h {0} -p 15002:wss -h {0} -p 15003".format(self.host))
        self.httpServer = None
        self.url = None
        self.driver = None
        try:
            cmd = "node -e \"require('./bin/HttpServer')()\"";
            cwd = current.testcase.getMapping().getPath()
            self.httpServer = Expect.Expect(cmd, cwd=cwd)
            self.httpServer.expect("listening on ports")

            if current.config.browser.startswith("Remote:"):
                from selenium import webdriver
                from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
                (driver, capabilities, port) = current.config.browser.split(":")
                self.driver = webdriver.Remote("http://localhost:{0}".format(port),
                                               getattr(DesiredCapabilities, capabilities))
            elif current.config.browser != "Manual":
                from selenium import webdriver
                if not hasattr(webdriver, current.config.browser):
                    raise RuntimeError("unknown browser `{0}'".format(current.config.browser))

                if current.config.browser == "Firefox":
                    if isinstance(platform, Linux) and os.environ.get("DISPLAY", "") != ":1" and os.environ.get("USER", "") == "ubuntu":
                        current.writeln("error: DISPLAY is unset, setting it to :1")
                        os.environ["DISPLAY"] = ":1"

                    #
                    # We need to specify a profile for Firefox. This profile only provides the cert8.db which
                    # contains our Test CA cert. It should be possible to avoid this by setting the webdriver
                    # acceptInsecureCerts capability but it's only supported by latest Firefox releases.
                    #
                    profile = webdriver.FirefoxProfile(os.path.join(toplevel, "scripts", "selenium", "firefox"))
                    self.driver = webdriver.Firefox(firefox_profile=profile)
                elif current.config.browser == "Ie":
                    # Make sure we start with a clean cache
                    capabilities = webdriver.DesiredCapabilities.INTERNETEXPLORER.copy()
                    capabilities["ie.ensureCleanSession"] = True
                    self.driver = webdriver.Ie(capabilities=capabilities)
                else:
                    self.driver = getattr(webdriver, current.config.browser)()
        except:
            self.destroy(current.driver)
            raise

    def __str__(self):
        return str(self.driver) if self.driver else "Manual"

    def getControllerIdentity(self, current):
        #
        # Load the controller page each time we're asked for the controller and if we're running
        # another testcase, the controller page will connect to the process controller registry
        # to register itself with this script.
        #
        testsuite = ("es5/" if current.config.es5 else "") + str(current.testsuite)
        if current.config.protocol == "wss":
            protocol = "https"
            port = "9090"
            cport = "15003"
        else:
            protocol = "http"
            port = "8080"
            cport = "15002"
        url = "{0}://{5}:{1}/test/{2}/controller.html?port={3}&worker={4}".format(protocol,
                                                                                  port,
                                                                                  testsuite,
                                                                                  cport,
                                                                                  current.config.worker,
                                                                                  self.host)
        if url != self.url:
            self.url = url
            if self.driver:
                self.driver.get(url)
            else:
                # If no process controller is registered, we request the user to load the controller
                # page in the browser. Once loaded, the controller will register and we'll redirect to
                # the correct testsuite page.
                ident = current.driver.getCommunicator().stringToIdentity("Browser/ProcessController")
                prx = None
                with self.cond:
                    while True:
                        if ident in self.processControllerProxies:
                            prx = self.processControllerProxies[ident]
                            break
                        print("Please load http://{0}:8080/{1}".format(self.host,
                                                                       "es5/start" if current.config.es5 else "start"))
                        self.cond.wait(5)

                try:
                    import Test
                    Test.Common.BrowserProcessControllerPrx.uncheckedCast(prx).redirect(url)
                except:
                    pass
                finally:
                    self.clearProcessController(prx, prx.ice_getCachedConnection())

        return "Browser/ProcessController"

    def getController(self, current):
        try:
            return RemoteProcessController.getController(self, current)
        except RuntimeError as ex:
            if self.driver:
                # Print out the client & server console element values
                for element in ["clientConsole", "serverConsole"]:
                    try:
                        console = self.driver.find_element_by_id(element).get_attribute('value')
                        if len(console) > 0:
                            print("controller {0} value:\n{1}".format(element, console))
                    except Exception as exc:
                        print("couldn't get controller {0} value:\n{1}".format(element, exc))
                        pass
                # Print out the browser log
                try:
                    print("browser log:\n{0}".format(self.driver.get_log("browser")))
                except:
                    pass # Not all browsers support retrieving the browser console log
            raise ex

    def destroy(self, driver):
        if self.httpServer:
            self.httpServer.terminate()
            self.httpServer = None

        try:
            self.driver.quit()
        except:
            pass

class Driver:

    class Current:

        def __init__(self, driver, testsuite, result):
            self.driver = driver
            self.testsuite = testsuite
            self.config = driver.configs[testsuite.getMapping()]
            self.desc = ""
            self.result = result
            self.host = None
            self.testcase = None
            self.testcases = []
            self.processes = {}
            self.dirs = []
            self.files = []

        def getTestEndpoint(self, *args, **kargs):
            return self.driver.getTestEndpoint(*args, **kargs)

        def getBuildDir(self, name):
            return self.testcase.getMapping().getBuildDir(name, self)

        def getPluginEntryPoint(self, plugin, process):
            return self.testcase.getMapping().getPluginEntryPoint(plugin, process, self)

        def write(self, *args, **kargs):
            self.result.write(*args, **kargs)

        def writeln(self, *args, **kargs):
            self.result.writeln(*args, **kargs)

        def push(self, testcase, host=None):
            if not testcase.mapping:
                assert(not testcase.parent and not testcase.testsuite)
                testcase.mapping = self.testcase.getMapping()
                testcase.testsuite = self.testcase.getTestSuite()
                testcase.parent = self.testcase
            self.testcases.append((self.testcase, self.config, self.host))
            self.testcase = testcase
            self.config = self.driver.configs[self.testcase.getMapping()].cloneAndOverrideWith(self)
            self.host = host

        def pop(self):
            assert(self.testcase)
            testcase = self.testcase
            (self.testcase, self.config, self.host) = self.testcases.pop()
            if testcase.parent and self.testcase != testcase:
                testcase.mapping = None
                testcase.testsuite = None
                testcase.parent = None

        def createFile(self, path, lines, encoding=None):
            path = os.path.join(self.testsuite.getPath(), path.decode("utf-8") if isPython2 else path)
            with open(path, "w", encoding=encoding) if not isPython2 and encoding else open(path, "w") as file:
                for l in lines:
                    file.write("%s\n" % l)
            self.files.append(path)

        def mkdirs(self, dirs):
            for d in dirs if isinstance(dirs, list) else [dirs]:
                d = os.path.join(self.testsuite.getPath(), d)
                self.dirs.append(d)
                if not os.path.exists(d):
                    os.makedirs(d)

        def destroy(self):
            for d in self.dirs:
                if os.path.exists(d): shutil.rmtree(d)
            for f in self.files:
                if os.path.exists(f): os.unlink(f)

    drivers = {}
    driver = "local"

    @classmethod
    def add(self, name, driver, default=False):
        if default:
            Driver.driver = name
        self.driver = name
        self.drivers[name] = driver

    @classmethod
    def getAll(self):
        return list(self.drivers.values())

    @classmethod
    def create(self, options):
        parseOptions(self, options)
        driver = self.drivers.get(self.driver)
        if not driver:
            raise RuntimeError("unknown driver `{0}'".format(self.driver))
        return driver(options)

    @classmethod
    def getSupportedArgs(self):
        return ("dlrR", ["debug", "driver=", "filter=", "rfilter=", "host=", "host-ipv6=", "host-bt=", "interface=",
                         "controller-app", "valgrind", "languages=", "rlanguages="])

    @classmethod
    def usage(self):
        pass

    @classmethod
    def commonUsage(self):
        print("")
        print("Driver options:")
        print("-d | --debug          Verbose information.")
        print("--driver=<driver>     Use the given driver (local, client, server or remote).")
        print("--filter=<regex>      Run all the tests that match the given regex.")
        print("--rfilter=<regex>     Run all the tests that do not match the given regex.")
        print("--languages=l1,l2,... List of comma-separated language mappings to test.")
        print("--rlanguages=l1,l2,.. List of comma-separated language mappings to not test.")
        print("--host=<addr>         The IPv4 address to use for Ice.Default.Host.")
        print("--host-ipv6=<addr>    The IPv6 address to use for Ice.Default.Host.")
        print("--host-bt=<addr>      The Bluetooth address to use for Ice.Default.Host.")
        print("--interface=<IP>      The multicast interface to use to discover controllers.")
        print("--controller-app      Start the process controller application.")
        print("--valgrind            Start executables with valgrind.")

    def __init__(self, options):
        self.debug = False
        self.filters = []
        self.rfilters = []
        self.host = ""
        self.hostIPv6 = ""
        self.hostBT = ""
        self.controllerApp = False
        self.valgrind = False
        self.languages = ",".join(os.environ.get("LANGUAGES", "").split(" "))
        self.languages = [self.languages] if self.languages else []
        self.rlanguages = []

        self.failures = []
        parseOptions(self, options, { "d": "debug",
                                      "r" : "filters",
                                      "R" : "rfilters",
                                      "filter" : "filters",
                                      "rfilter" : "rfilters",
                                      "host-ipv6" : "hostIPv6",
                                      "host-bt" : "hostBT",
                                      "controller-app" : "controllerApp"})

        self.filters = [re.compile(a) for a in self.filters]
        self.rfilters = [re.compile(a) for a in self.rfilters]
        if self.languages:
            self.languages = [i for sublist in [l.split(",") for l in self.languages] for i in sublist]
        if self.rlanguages:
            self.rlanguages = [i for sublist in [l.split(",") for l in self.rlanguages] for i in sublist]

        self.communicator = None
        self.interface = ""
        self.processControllers = {}

    def setConfigs(self, configs):
        self.configs = configs

    def useIceBinDist(self, mapping):
        return self.useBinDist(mapping, "ICE_BIN_DIST")

    def getIceDir(self, mapping, current):
        return self.getInstallDir(mapping, current, "ICE_HOME", "ICE_BIN_DIST")

    def useBinDist(self, mapping, envName):
        env = os.environ.get(envName, "").split()
        return 'all' in env or mapping.name in env

    def getInstallDir(self, mapping, current, envHomeName, envBinDistName):
        if self.useBinDist(mapping, envBinDistName):
            if envHomeName == "ICE_HOME":
                return platform.getIceInstallDir(mapping, current)
            else:
                return platform.getInstallDir(mapping, current, envHomeName)
        elif mapping:
            return mapping.getPath()
        else:
            return toplevel

    def getSliceDir(self, mapping, current):
        return platform.getSliceDir(self.getIceDir(mapping, current) if self.useIceBinDist(mapping) else toplevel)

    def isWorkerThread(self):
        return False

    def getTestEndpoint(self, portnum, protocol="default"):
        return "{0} -p {1}".format(protocol, self.getTestPort(portnum))

    def getTestPort(self, portnum):
        return 12010 + portnum

    def getArgs(self, process, current):
        ### Return driver specific arguments
        return []

    def getProps(self, process, current):
        props = {}
        if isinstance(process, IceProcess):
            if not self.host:
                props["Ice.Default.Host"] = "0:0:0:0:0:0:0:1" if current.config.ipv6 else "127.0.0.1"
            else:
                props["Ice.Default.Host"] = self.host
        return props

    def getMappings(self):
        ### Return additional mappings to load required by the driver
        return []

    def matchLanguage(self, language):
        if self.languages and language not in self.languages:
            return False
        if self.rlanguages and language in self.rlanguages:
            return False
        return True

    def getCommunicator(self):
        self.initCommunicator()
        return self.communicator

    def initCommunicator(self):
        if self.communicator:
            return

        try:
            import Ice
        except ImportError:
            # Try to add the local Python build to the sys.path
            pythonMapping = Mapping.getByName("python")
            if pythonMapping:
                for p in pythonMapping.getPythonDirs(pythonMapping.getPath(), self.configs[pythonMapping]):
                    sys.path.append(p)

        import Ice
        Ice.loadSlice(os.path.join(toplevel, "scripts", "Controller.ice"))

        initData = Ice.InitializationData()
        initData.properties = Ice.createProperties()

        # Load IceSSL, this is useful to talk with WSS for JavaScript
        initData.properties.setProperty("Ice.Plugin.IceSSL", "IceSSL:createIceSSL")
        initData.properties.setProperty("IceSSL.DefaultDir", os.path.join(toplevel, "certs"))
        initData.properties.setProperty("IceSSL.CertFile", "server.p12")
        initData.properties.setProperty("IceSSL.Password", "password")
        initData.properties.setProperty("IceSSL.Keychain", "test.keychain")
        initData.properties.setProperty("IceSSL.KeychainPassword", "password")
        initData.properties.setProperty("IceSSL.VerifyPeer", "0");

        initData.properties.setProperty("Ice.Plugin.IceDiscovery", "IceDiscovery:createIceDiscovery")
        initData.properties.setProperty("IceDiscovery.DomainId", "TestController")
        initData.properties.setProperty("IceDiscovery.Interface", self.interface)
        initData.properties.setProperty("Ice.Default.Host", self.interface)
        initData.properties.setProperty("Ice.ThreadPool.Server.Size", "10")
        #initData.properties.setProperty("Ice.Trace.Protocol", "1")
        #initData.properties.setProperty("Ice.Trace.Network", "3")
        #initData.properties.setProperty("Ice.StdErr", "allTests.log")
        initData.properties.setProperty("Ice.Override.Timeout", "10000")
        initData.properties.setProperty("Ice.Override.ConnectTimeout", "1000")
        self.communicator = Ice.initialize(initData)

    def getProcessController(self, current, process=None):
        processController = None
        if current.config.buildPlatform == "iphonesimulator":
            processController = iOSSimulatorProcessController
        elif current.config.buildPlatform == "iphoneos":
            processController = iOSDeviceProcessController
        elif current.config.uwp:
            # No SSL server-side support in UWP.
            if current.config.protocol in ["ssl", "wss"] and not isinstance(process, Client):
                processController = LocalProcessController
            else:
                processController = UWPProcessController
        elif process and isinstance(process.getMapping(current), JavaScriptMapping) and current.config.browser:
            processController = BrowserProcessController
        elif process and (isinstance(process.getMapping(current), AndroidMapping) or
                          isinstance(process.getMapping(current), AndroidCompatMapping)):
            processController = AndroidProcessController
        else:
            processController = LocalProcessController

        if processController in self.processControllers:
            return self.processControllers[processController]

        # Instantiate the controller
        self.processControllers[processController] = processController(current)
        return self.processControllers[processController]

    def getProcessProps(self, current, ready, readyCount):
        props = {}
        if ready or readyCount > 0:
            if current.config.buildPlatform not in ["iphonesimulator", "iphoneos"]:
                props["Ice.PrintAdapterReady"] = 1
        return props

    def destroy(self):
        for controller in self.processControllers.values():
            controller.destroy(self)

        if self.communicator:
            self.communicator.destroy()

class CppMapping(Mapping):

    class Config(Mapping.Config):

        @classmethod
        def getSupportedArgs(self):
            return ("", ["cpp-config=", "cpp-platform=", "uwp", "openssl"])

        @classmethod
        def usage(self):
            print("")
            print("C++ Mapping options:")
            print("--cpp-config=<config>     C++ build configuration for native executables (overrides --config).")
            print("--cpp-platform=<platform> C++ build platform for native executables (overrides --platform).")
            print("--uwp                     Run UWP (Universal Windows Platform).")
            print("--openssl                 Run SSL tests with OpenSSL instead of the default platform SSL engine.")

        def __init__(self, options=[]):
            Mapping.Config.__init__(self, options)

            # Derive from the build config the cpp11 option. This is used by canRun to allow filtering
            # tests on the cpp11 value in the testcase options specification
            self.cpp11 = self.buildConfig.lower().find("cpp11") >= 0

            parseOptions(self, options, { "cpp-config" : "buildConfig", "cpp-platform" : "buildPlatform" })

        def canRun(self, current):
            if not Mapping.Config.canRun(self, current):
                return False

            # No C++11 tests for IceStorm, IceGrid, etc
            if self.cpp11:
                testId = current.testcase.getTestSuite().getId()
                parent = re.match(r'^([\w]*).*', testId).group(1)
                if parent in ["IceStorm", "IceBridge"]:
                    return False
                elif parent in ["IceGrid"] and testId not in ["IceGrid/simple"]:
                    return False
                elif parent in ["Glacier2"] and testId not in ["Glacier2/application", "Glacier2/sessionHelper"]:
                    return False
            return True

    def getNugetPackage(self, compiler, version):
        return "zeroc.ice.{0}.{1}".format(compiler, version)

    def getDefaultExe(self, processType, config):
        return platform.getDefaultExe(processType, config)

    def getOptions(self, current):
        return { "compress" : [False] } if current.config.uwp else {}

    def getProps(self, process, current):
        props = Mapping.getProps(self, process, current)
        if isinstance(process, IceProcess):
            props["Ice.NullHandleAbort"] = True
            props["Ice.PrintStackTraces"] = "1"
        return props

    def getSSLProps(self, process, current):
        props = Mapping.getSSLProps(self, process, current)
        server = isinstance(process, Server)
        uwp = current.config.uwp

        props.update({
            "IceSSL.CAs": "cacert.pem",
            "IceSSL.CertFile": "server.p12" if server else "ms-appx:///client.p12" if uwp else "client.p12"
        })
        if isinstance(platform, Darwin):
            keychainFile = "server.keychain" if server else "client.keychain"
            props.update({
                "IceSSL.KeychainPassword" : "password",
                "IceSSL.Keychain": keychainFile
             })
        return props

    def getPluginEntryPoint(self, plugin, process, current):
        return {
            "IceSSL" : "IceSSLOpenSSL:createIceSSLOpenSSL" if current.config.openssl else "IceSSL:createIceSSL",
            "IceBT" : "IceBT:createIceBT",
            "IceDiscovery" : "IceDiscovery:createIceDiscovery",
            "IceLocatorDiscovery" : "IceLocatorDiscovery:createIceLocatorDiscovery"
        }[plugin]

    def getEnv(self, process, current):
        #
        # On Windows, add the testcommon directories to the PATH
        #
        libPaths = []
        if isinstance(platform, Windows):
            testcommon = os.path.join(self.path, "test", "Common")
            if os.path.exists(testcommon):
                libPaths.append(os.path.join(testcommon, self.getBuildDir("testcommon", current)))

        #
        # On most platforms, we also need to add the library directory to the library path environment variable.
        #
        if not isinstance(platform, Darwin):
            libPaths.append(self.getLibDir(process, current))

        #
        # Add the test suite library directories to the platform library path environment variable.
        #
        if current.testcase:
            for d in set([current.getBuildDir(d) for d in current.testcase.getTestSuite().getLibDirs()]):
                libPaths.append(d)

        env = {}
        if len(libPaths) > 0:
            env[platform.getLdPathEnvName()] = os.pathsep.join(libPaths)
        return env

    def getDefaultSource(self, processType):
        return {
            "client" : "Client.cpp",
            "server" : "Server.cpp",
            "serveramd" : "ServerAMD.cpp",
            "collocated" : "Collocated.cpp",
        }[processType]

class JavaMapping(Mapping):

    def getCommandLine(self, current, process, exe, args):
        javaHome = os.getenv("JAVA_HOME", "")
        java = os.path.join(javaHome, "bin", "java") if javaHome else "java"
        if process.isFromBinDir():
            return "{0} {1} {2}".format(java, exe, args)

        assert(current.testcase.getPath().startswith(self.getTestsPath()))
        package = "test." + current.testcase.getPath()[len(self.getTestsPath()) + 1:].replace(os.sep, ".")
        javaArgs = self.getJavaArgs(process, current)
        if javaArgs:
            return "{0} {1} {2}.{3} {4}".format(java, " ".join(javaArgs), package, exe, args)
        else:
            return "{0} {1}.{2} {3}".format(java, package, exe, args)

    def getJavaArgs(self, process, current):
        return []

    def getSSLProps(self, process, current):
        props = Mapping.getSSLProps(self, process, current)
        props.update({
            "IceSSL.Keystore": "server.jks" if isinstance(process, Server) else "client.jks",
        })
        return props

    def getPluginEntryPoint(self, plugin, process, current):
        return {
            "IceSSL" : "com.zeroc.IceSSL.PluginFactory",
            "IceBT" : "com.zeroc.IceBT.PluginFactory",
            "IceDiscovery" : "com.zeroc.IceDiscovery.PluginFactory",
            "IceLocatorDiscovery" : "com.zeroc.IceLocatorDiscovery.PluginFactory"
        }[plugin]

    def getEnv(self, process, current):
        return { "CLASSPATH" : os.path.join(self.path, "lib", "test.jar") }

    def getTestsPath(self):
        return os.path.join(self.path, "test/src/main/java/test")

    def getDefaultSource(self, processType):
        return self.getDefaultExe(processType) + ".java"

    def getDefaultExe(self, processType, config=None):
        return {
            "client" : "Client",
            "server" : "Server",
            "serveramd" : "AMDServer",
            "collocated" : "Collocated",
            "icebox": "com.zeroc.IceBox.Server",
            "iceboxadmin" : "com.zeroc.IceBox.Admin",
        }[processType]

class JavaCompatMapping(JavaMapping):

    def getPluginEntryPoint(self, plugin, process, current):
        return {
            "IceSSL" : "IceSSL.PluginFactory",
            "IceBT" : "IceBT.PluginFactory",
            "IceDiscovery" : "IceDiscovery.PluginFactory",
            "IceLocatorDiscovery" : "IceLocatorDiscovery.PluginFactory"
        }[plugin]

    def getDefaultExe(self, processType, config=None):
        return {
            "client" : "Client",
            "server" : "Server",
            "serveramd" : "AMDServer",
            "collocated" : "Collocated",
            "icebox": "IceBox.Server",
            "iceboxadmin" : "IceBox.Admin",
        }[processType]

class Android:

    @classmethod
    def getUnsuportedTests(self, protocol):
        tests = [
            "Ice/hash",
            "Ice/faultTolerance",
            "Ice/metrics",
            "Ice/networkProxy",
            "Ice/throughput",
            "Ice/plugin",
            "Ice/logger",
            "Ice/properties"]
        if protocol in ["ssl", "wss"]:
            tests += [
                "Ice/binding",
                "Ice/timeout",
                "Ice/udp"]
        if protocol in ["bt", "bts"]:
            tests += [
                "Ice/info",
                "Ice/udp"]
        return tests

class AndroidMapping(JavaMapping):

    class Config(Mapping.Config):

        @classmethod
        def getSupportedArgs(self):
            return ("", ["device=", "avd=", "androidemulator"])

        @classmethod
        def usage(self):
            print("")
            print("Android Mapping options:")
            print("--device=<device-id>      ID of the emulator or device used to run the tests.")
            print("--androidemulator         Run tests in emulator as opposed to a real device.")
            print("--avd=<name>              Start specific Android Virtual Device")

        def __init__(self, options=[]):
            Mapping.Config.__init__(self, options)

            parseOptions(self, options)
            self.androidemulator = self.androidemulator or self.avd

    def getSSLProps(self, process, current):
        props = JavaMapping.getSSLProps(self, process, current)
        props.update({
            "IceSSL.KeystoreType" : "BKS",
            "IceSSL.TruststoreType" : "BKS",
            "Ice.InitPlugins" : "0",
            "IceSSL.Keystore": "server.bks" if isinstance(process, Server) else "client.bks"})
        return props

    def getTestsPath(self):
        return os.path.join(self.path, "../test/src/main/java/test")

    def getCommonTestsPath(self):
        return os.path.join(self.path, "..", "..", "scripts", "tests")

    def filterTestSuite(self, testId, config, filters=[], rfilters=[]):
        if not testId.startswith("Ice/") or testId in Android.getUnsuportedTests(config.protocol):
            return True
        return JavaMapping.filterTestSuite(self, testId, config, filters, rfilters)

    def getSDKPackage(self):
        return "system-images;android-25;google_apis;x86_64"

class AndroidCompatMapping(JavaCompatMapping):

    class Config(Mapping.Config):

        @classmethod
        def getSupportedArgs(self):
            return ("", ["device=", "avd=", "androidemulator"])

        @classmethod
        def usage(self):
            print("")
            print("Android Mapping options:")
            print("--device=<device-id>      Id of the emulator or device used to run the tests.")
            print("--androidemulator         Run tests in emulator as opposed to a real device.")
            print("--avd                     Start emulator image")

        def __init__(self, options=[]):
            Mapping.Config.__init__(self, options)

            parseOptions(self, options)
            self.androidemulator = self.androidemulator or self.avd

    def getSSLProps(self, process, current):
        props = JavaCompatMapping.getSSLProps(self, process, current)
        props.update({
            "IceSSL.KeystoreType" : "BKS",
            "IceSSL.TruststoreType" : "BKS",
            "Ice.InitPlugins" : "0",
            "IceSSL.Keystore": "server.bks" if isinstance(process, Server) else "client.bks"})
        return props

    def getTestsPath(self):
        return os.path.join(self.path, "../test/src/main/java/test")

    def getCommonTestsPath(self):
        return os.path.join(self.path, "..", "..", "scripts", "tests")

    def filterTestSuite(self, testId, config, filters=[], rfilters=[]):
        if not testId.startswith("Ice/") or testId in Android.getUnsuportedTests(config.protocol):
            return True
        return JavaCompatMapping.filterTestSuite(self, testId, config, filters, rfilters)

    def getSDKPackage(self):
        return "system-images;android-21;google_apis;x86_64"

class CSharpMapping(Mapping):

    class Config(Mapping.Config):

        @classmethod
        def getSupportedArgs(self):
            return ("", ["netframework="])

        @classmethod
        def usage(self):
            print("")
            print("--netframework           Run C# tests using Ice netstandard2.0 libraries and tests")
            print("                         build with given .NET Framework [netcoreapp2.0|net4.6]")

        def __init__(self, options=[]):
            Mapping.Config.__init__(self, options)
            parseOptions(self, options, { "netframework" : "netframework" })
            #
            # For non Windows platforms the default is netcoreapp2.0 for windows empty
            # means to run test agains .NET Framework 4.5 Ice build
            #
            supportedframeworks = ["netcoreapp2.0"]
            if isinstance(platform, Windows):
                supportedframeworks += ["net461", "net462", "net47", "net471"]

            if self.netframework:
                if not self.netframework in supportedframeworks:
                    raise RuntimeError("Unssuported .NET Framework `{0}'".format(self.netframework))
            else:
                self.netframework = "" if isinstance(platform, Windows) else "netcoreapp2.0"

        def canRun(self, current):
            testId = current.testcase.getTestSuite().getId()
            if not Mapping.Config.canRun(self, current):
                return False

            if self.netframework:
                #
                # The following tests require multicast, on Unix platforms it's currently only supported
                # with IPv4 due to .NET Core bug https://github.com/dotnet/corefx/issues/25525
                #
                if not isinstance(platform, Windows) and self.ipv6 and testId in ["Ice/udp",
                                                                                  "IceDiscovery/simple",
                                                                                  "IceGrid/simple"]:
                    return False

                if isinstance(platform, Darwin):
                    if "IceSSL" in testId or "IceDiscovery" in testId:
                        return False

                    # TODO: Remove once https://github.com/dotnet/corefx/issues/28759 is fixed
                    if testId == "Ice/adapterDeactivation" and self.protocol in ["ssl", "wss"]:
                        return False

            return True

    def getBuildDir(self, name, current):
        if current.config.netframework:
            return os.path.join("msbuild", name, "netstandard2.0", current.config.netframework)
        else:
            return os.path.join("msbuild", name, "net45")

    def getSSLProps(self, process, current):
        props = Mapping.getSSLProps(self, process, current)
        props.update({
            "IceSSL.Password": "password",
            "IceSSL.DefaultDir": os.path.join(toplevel, "certs"),
            "IceSSL.CAs": "cacert.pem",
            "IceSSL.VerifyPeer": "0" if current.config.protocol == "wss" else "2",
            "IceSSL.CertFile": "server.p12" if isinstance(process, Server) else "client.p12",
        })
        return props

    def getPluginEntryPoint(self, plugin, process, current):
        plugindir = "{0}/{1}".format(current.driver.getIceDir(self, current), "lib")

        if current.config.netframework:
            plugindir = os.path.join(plugindir, "netstandard2.0")
        else:
            plugindir = os.path.join(plugindir, "net45")

        #
        # If the plug-in assemblie exists in the test directory, this is a good indication that the
        # test include a reference to the plug-in, in this case we must use the test dir as the plug-in
        # base directory to avoid loading two instances of the same assemblie.
        #
        proccessType = current.testcase.getProcessType(process)
        if proccessType:
            testdir = os.path.join(current.testcase.getPath(), self.getBuildDir(proccessType, current))
            if os.path.isfile(os.path.join(testdir, plugin + ".dll")):
                plugindir = testdir

        return {
            "IceSSL" : plugindir + "/IceSSL.dll:IceSSL.PluginFactory",
            "IceDiscovery" : plugindir + "/IceDiscovery.dll:IceDiscovery.PluginFactory",
            "IceLocatorDiscovery" : plugindir + "/IceLocatorDiscovery.dll:IceLocatorDiscovery.PluginFactory"
        }[plugin]

    def getEnv(self, process, current):
        env = {}
        if isinstance(platform, Windows):

            if current.driver.useIceBinDist(self):
                framework = "net45" if not current.config.netframework else current.config.netframework
                env['PATH'] = os.path.join(platform.getIceInstallDir(self, current), "tools", framework)
                if not current.config.netframework:
                    env['DEVPATH'] = os.path.join(platform.getIceInstallDir(self, current), "lib", "net45")
            else:
                env['PATH'] = os.path.join(toplevel, "cpp", "msbuild", "packages",
                                           "bzip2.{0}.1.0.6.10".format(platform.getPlatformToolset()),
                                           "build", "native", "bin", "x64", "Release")
                if not current.config.netframework:
                    env['DEVPATH'] = os.path.join(current.driver.getIceDir(self, current), "lib", "net45")
        return env

    def getDefaultSource(self, processType):
        return {
            "client" : "Client.cs",
            "server" : "Server.cs",
            "serveramd" : "ServerAMD.cs",
            "collocated" : "Collocated.cs",
        }[processType]

    def getDefaultExe(self, processType, config):
        return processType

    def getNugetPackage(self, compiler, version):
        return "zeroc.ice.net.{0}".format(version)

    def getCommandLine(self, current, process, exe, args):
        cmd = ""
        if current.config.netframework:
            cmd = "dotnet "
            if exe  == "icebox":
                if current.driver.useIceBinDist(self):
                    cmd += os.path.join(platform.getIceInstallDir(self, current), "tools", "netcoreapp2.0", "iceboxnet.dll")
                else:
                    cmd += os.path.join(self.path, "bin", "netcoreapp2.0", "iceboxnet.dll")
            else:
                if current.config.netframework == "netcoreapp2.0":
                    cmd += os.path.join(current.testcase.getPath(), self.getBuildDir(exe, current), "{0}.dll".format(exe))
                else:
                    cmd += os.path.join(current.testcase.getPath(), self.getBuildDir(exe, current), "{0}".format(exe))
        else:
            if exe == "icebox":
                if current.driver.useIceBinDist(self):
                    cmd = os.path.join(platform.getIceInstallDir(self, current), "tools", "net45", "iceboxnet.exe")
                else:
                    cmd = os.path.join(self.path, "bin", "net45", "iceboxnet.exe")
            else:
                cmd = os.path.join(current.testcase.getPath(), self.getBuildDir(exe, current), exe)
        return cmd + " " + args

class CppBasedMapping(Mapping):

    class Config(Mapping.Config):

        @classmethod
        def getSupportedArgs(self):
            return ("", [self.mappingName + "-config=", self.mappingName + "-platform=", "openssl"])

        @classmethod
        def usage(self):
            print("")
            print(self.mappingDesc + " mapping options:")
            print("--{0}-config=<config>     {1} build configuration for native executables (overrides --config)."
                .format(self.mappingName, self.mappingDesc))
            print("--{0}-platform=<platform> {1} build platform for native executables (overrides --platform)."
                .format(self.mappingName, self.mappingDesc))
            print("--openssl                 Run SSL tests with OpenSSL instead of the default platform SSL engine.")

        def __init__(self, options=[]):
            Mapping.Config.__init__(self, options)
            parseOptions(self, options,
                { self.mappingName + "-config" : "buildConfig",
                  self.mappingName + "-platform" : "buildPlatform" })

    def getSSLProps(self, process, current):
        return Mapping.getByName("cpp").getSSLProps(process, current)

    def getPluginEntryPoint(self, plugin, process, current):
        return Mapping.getByName("cpp").getPluginEntryPoint(plugin, process, current)

    def getEnv(self, process, current):
        env = Mapping.getEnv(self, process, current)
        if current.driver.getIceDir(self, current) != platform.getIceInstallDir(self, current):
            # If not installed in the default platform installation directory, add
            # the Ice C++ library directory to the library path
            env[platform.getLdPathEnvName()] = Mapping.getByName("cpp").getLibDir(process, current)
        return env

    def getNugetPackage(self, compiler, version):
        return "zeroc.ice.{0}.{1}".format(compiler, version)

class ObjCMapping(CppBasedMapping):

    def getTestSuites(self, ids=[]):
        return Mapping.getTestSuites(self, ids) if isinstance(platform, Darwin) else []

    def findTestSuite(self, testsuite):
        return Mapping.findTestSuite(self, testsuite) if isinstance(platform, Darwin) else None

    class Config(CppBasedMapping.Config):
        mappingName = "objc"
        mappingDesc = "Objective-C"

        def __init__(self, options=[]):
            Mapping.Config.__init__(self, options)
            self.arc = self.buildConfig.lower().find("arc") >= 0

    def getDefaultSource(self, processType):
        return {
            "client" : "Client.m",
            "server" : "Server.m",
            "collocated" : "Collocated.m",
        }[processType]

class PythonMapping(CppBasedMapping):

    class Config(CppBasedMapping.Config):
        mappingName = "python"
        mappingDesc = "Python"

    def getCommandLine(self, current, process, exe, args):
        return "\"{0}\" {1} {2}".format(sys.executable, exe, args)

    def getEnv(self, process, current):
        env = CppBasedMapping.getEnv(self, process, current)
        if current.driver.getIceDir(self, current) != platform.getIceInstallDir(self, current):
            # If not installed in the default platform installation directory, add
            # the Ice python directory to PYTHONPATH
            dirs = self.getPythonDirs(current.driver.getIceDir(self, current), current.config)
            env["PYTHONPATH"] = os.pathsep.join(dirs)
        return env

    def getPythonDirs(self, iceDir, config):
        dirs = []
        if isinstance(platform, Windows):
            dirs.append(os.path.join(iceDir, "python", config.buildPlatform, config.buildConfig))
        dirs.append(os.path.join(iceDir, "python"))
        return dirs

    def getDefaultExe(self, processType, config):
        return self.getDefaultSource(processType)

    def getDefaultSource(self, processType):
        return {
            "client" : "Client.py",
            "server" : "Server.py",
            "serveramd" : "ServerAMD.py",
            "collocated" : "Collocated.py",
        }[processType]

class CppBasedClientMapping(CppBasedMapping):

    def loadTestSuites(self, tests, config, filters, rfilters):
        Mapping.loadTestSuites(self, tests, config, filters, rfilters)
        self.getServerMapping().loadTestSuites(self.testsuites.keys(), config)

    def getServerMapping(self, testId=None):
        return Mapping.getByName("cpp") # By default, run clients against C++ mapping executables

    def getDefaultExe(self, processType, config):
        return self.getDefaultSource(processType)

class RubyMapping(CppBasedClientMapping):

    class Config(CppBasedClientMapping.Config):
        mappingName = "ruby"
        mappingDesc = "Ruby"

    def getCommandLine(self, current, process, exe, args):
        return "ruby " + exe + " " + args

    def getEnv(self, process, current):
        env = CppBasedMapping.getEnv(self, process, current)
        if current.driver.getIceDir(self, current) != platform.getIceInstallDir(self, current):
            # If not installed in the default platform installation directory, add
            # the Ice ruby directory to RUBYLIB
            env["RUBYLIB"] = os.path.join(self.path, "ruby")
        return env

    def getDefaultSource(self, processType):
        return { "client" : "Client.rb" }[processType]

class PhpMapping(CppBasedClientMapping):

    class Config(CppBasedClientMapping.Config):
        mappingName = "php"
        mappingDesc = "PHP"

    def getCommandLine(self, current, process, exe, args):
        phpArgs = []
        php = "php"
        #
        # If Ice is not installed in the system directory, specify its location with PHP
        # configuration arguments.
        #
        if isinstance(platform, Windows):
            systemInstall = current.driver.useIceBinDist(self)
        else:
            systemInstall = current.driver.getIceDir(self, current) == platform.getIceInstallDir(self, current)

        if not systemInstall:
            if isinstance(platform, Windows):
                buildPlatform = current.driver.configs[self].buildPlatform
                buildConfig = current.driver.configs[self].buildConfig
                packageName = "php-7.1-ts.7.1.17" if buildConfig in ["Debug", "Release"] else "php-7.1-nts.7.1.17"
                extension = "php_ice.dll" if buildConfig in ["Debug", "Release"] else "php_ice_nts.dll"
                buildConfig = "Debug" if current.driver.configs[self].buildConfig.find("Debug") >= 0 else "Release"

                php = os.path.join(self.path, "msbuild", "packages", packageName, "build", "native", "bin",
                                   buildPlatform, buildConfig, "php.exe")

                extensionDir = os.path.join(self.path, "lib", buildPlatform, buildConfig)
                includePath = self.getLibDir(process, current)
            else:
                useBinDist = current.driver.useIceBinDist(self)
                extension = "ice.so"
                extensionDir = self.getLibDir(process, current)
                includePath = "{0}/{1}".format(current.driver.getIceDir(self, current), "php" if useBinDist else "lib")

            phpArgs += ["-n"] # Do not load any php.ini files
            phpArgs += ["-d", "extension_dir='{0}'".format(extensionDir)]
            phpArgs += ["-d", "extension='{0}'".format(extension)]
            phpArgs += ["-d", "include_path='{0}'".format(includePath)]
        if hasattr(process, "getPhpArgs"):
            phpArgs += process.getPhpArgs(current)
        return "{0} {1} -f {2} -- {3}".format(php, " ".join(phpArgs), exe, args)

    def getDefaultSource(self, processType):
        return { "client" : "Client.php" }[processType]

class MatlabMapping(CppBasedClientMapping):

    class Config(CppBasedClientMapping.Config):
        mappingName = "matlab"
        mappingDesc = "MATLAB"

    def getCommandLine(self, current, process, exe, args):
        return "matlab -nodesktop -nosplash -wait -log -minimize -r \"cd '{0}', runTest {1} {2} {3}\"".format(
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "matlab", "test", "lib")),
            self.getTestCwd(process, current),
            os.path.join(current.config.buildPlatform, current.config.buildConfig),
            args)

    def getServerMapping(self, testId=None):
        return Mapping.getByName("python") # Run clients against Python mapping servers

    def getDefaultSource(self, processType):
        return { "client" : "client.m" }[processType]

    def getOptions(self, current):
        #
        # Metrics tests configuration not supported with MATLAB they use the Admin adapter.
        #
        options = CppBasedClientMapping.getOptions(self, current)
        options["mx"] = [ False ]
        return options

class JavaScriptMapping(Mapping):

    class Config(Mapping.Config):

        @classmethod
        def getSupportedArgs(self):
            return ("", ["es5", "browser=", "worker"])

        @classmethod
        def usage(self):
            print("")
            print("JavaScript mapping options:")
            print("--es5                 Use JavaScript ES5 (Babel compiled code).")
            print("--browser=<name>      Run with the given browser.")
            print("--worker              Run with Web workers enabled.")

        def __init__(self, options=[]):
            Mapping.Config.__init__(self, options)
            self.es5 = False
            self.browser = ""
            self.worker = False
            parseOptions(self, options)
            if self.browser and self.protocol == "tcp":
                self.protocol = "ws"

            # Ie only support ES5 for now
            if self.browser in ["Ie"]:
                self.es5 = True

    def loadTestSuites(self, tests, config, filters, rfilters):
        Mapping.loadTestSuites(self, tests, config, filters, rfilters)
        self.getServerMapping().loadTestSuites(list(self.testsuites.keys()) + ["Ice/echo"], config, filters, rfilters)

    def getServerMapping(self, testId=None):
        if testId and self.hasSource(testId, "server"):
            return self
        else:
            return Mapping.getByName("cpp") # Run clients against C++ mapping servers if no JS server provided

    def getDefaultProcesses(self, processType, testsuite):
        if processType in ["server", "serveramd"]:
            return [EchoServer(), Server()]
        return Mapping.getDefaultProcesses(self, processType, testsuite)

    def getCommandLine(self, current, process, exe, args):
        if current.config.es5:
            return "node {0}/test/es5/Common/run.js --es5 {1} {2}".format(self.path, exe, args)
        else:
            return "node {0}/test/Common/run.js {1} {2}".format(self.path, exe, args)

    def getDefaultSource(self, processType):
        return { "client" : "Client.js", "serveramd" : "ServerAMD.js", "server" : "Server.js" }[processType]

    def getDefaultExe(self, processType, config=None):
        return self.getDefaultSource(processType).replace(".js", "")

    def getEnv(self, process, current):
        env = Mapping.getEnv(self, process, current)
        env["NODE_PATH"] = self.getTestCwd(process, current)
        return env

    def getSSLProps(self, process, current):
        return {}

    def getTestCwd(self, process, current):
        if current.config.es5:
            # Change to the ES5 test directory if testing ES5
            return os.path.join(self.path, "test", "es5", current.testcase.getTestSuite().getId())
        else:
            return os.path.join(self.path, "test", current.testcase.getTestSuite().getId())

    def computeTestCases(self, testId, files):
        if testId.find("es5") > -1:
            return # Ignore es5 directories
        return Mapping.computeTestCases(self, testId, files)

    def getOptions(self, current):
        options = {
            "protocol" : ["ws", "wss"] if current.config.browser else ["tcp"],
            "compress" : [False],
            "ipv6" : [False],
            "serialize" : [False],
            "mx" : [False],
            "es5" : [True] if current.config.es5 else [False, True],
            "worker" : [False, True] if current.config.browser and current.config.browser != "Ie" else [False],
        }
        return options

try:
    from Glacier2Util import *
    from IceBoxUtil import *
    from IceBridgeUtil import *
    from IcePatch2Util import *
    from IceGridUtil import *
    from IceStormUtil import *
except ImportError:
    pass

from LocalDriver import *

#
# Supported mappings
#
for m in filter(lambda x: os.path.isdir(os.path.join(toplevel, x)), os.listdir(toplevel)):
    if m == "cpp" or re.match("cpp-.*", m):
        Mapping.add(m, CppMapping())
    elif m == "java-compat" or re.match("java-compat-.*", m):
        Mapping.add(m, JavaCompatMapping())
    elif m == "java" or re.match("java-.*", m):
        Mapping.add(m, JavaMapping())
    elif m == "python" or re.match("python-.*", m):
        Mapping.add(m, PythonMapping())
    elif m == "ruby" or re.match("ruby-.*", m):
        Mapping.add(m, RubyMapping())
    elif m == "php" or re.match("php-.*", m):
        Mapping.add(m, PhpMapping())
    elif m == "js" or re.match("js-.*", m):
        Mapping.add(m, JavaScriptMapping())
    elif m == "objective-c" or re.match("objective-c-*", m):
        Mapping.add(m, ObjCMapping())

try:
    if not isinstance(platform, Windows):
        #
        # Check if the .NET Core SDK is installed
        #
        run("dotnet --version")
    Mapping.add("csharp", CSharpMapping())
except:
    pass

#
# Check if the Android SDK is installed and eventually add the Android mappings
#
try:
    run("adb version")
    Mapping.add(os.path.join("java-compat", "android"), AndroidCompatMapping())
    Mapping.add(os.path.join("java", "android"), AndroidMapping())
except:
    pass

#
# Check if Matlab is installed and eventually add the Matlab mapping
#
try:
    run("where matlab" if isinstance(platform, Windows) else "which matlab")
    Mapping.add("matlab", MatlabMapping())
except:
    pass

def runTestsWithPath(path):
    runTests([Mapping.getByPath(path)])

def runTests(mappings=None, drivers=None):
    if not mappings:
        mappings = Mapping.getAll()
    if not drivers:
        drivers = Driver.getAll()

    def usage():
        print("Usage: " + sys.argv[0] + " [options] [tests]")
        print("")
        print("Options:")
        print("-h | --help        Show this message")

        Driver.commonUsage()
        for driver in drivers:
            driver.usage()

        Mapping.Config.commonUsage()
        for mapping in mappings:
            mapping.Config.usage()

        print("")

    driver = None
    try:
        options = [Driver.getSupportedArgs(), Mapping.Config.getSupportedArgs()]
        options += [driver.getSupportedArgs() for driver in drivers]
        options += [mapping.Config.getSupportedArgs() for mapping in Mapping.getAll()]
        shortOptions = "h"
        longOptions = ["help"]
        for so, lo in options:
            shortOptions += so
            longOptions += lo
        opts, args = getopt.gnu_getopt(sys.argv[1:], shortOptions, longOptions)

        for (o, a) in opts:
            if o in ["-h", "--help"]:
                usage()
                sys.exit(0)

        #
        # Create the driver
        #
        driver = Driver.create(opts)

        #
        # Create the configurations for each mapping.
        #
        configs = {}
        for mapping in Mapping.getAll():
            if mapping not in configs:
                configs[mapping] = mapping.createConfig(opts[:])

        #
        # If the user specified --languages/rlanguages, only run matching mappings.
        #
        mappings = [m for m in mappings if driver.matchLanguage(str(m))]

        #
        # Provide the configurations to the driver and load the test suites for each mapping.
        #
        driver.setConfigs(configs)
        for mapping in mappings + driver.getMappings():
            mapping.loadTestSuites(args, configs[mapping], driver.filters, driver.rfilters)

        #
        # Finally, run the test suites with the driver.
        #
        try:
            sys.exit(driver.run(mappings, args))
        except KeyboardInterrupt:
            pass
        finally:
            driver.destroy()

    except Exception as e:
        print(sys.argv[0] + ": unexpected exception raised:\n" + traceback.format_exc())
        sys.exit(1)
