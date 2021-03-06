// **********************************************************************
//
// Copyright (c) 2003-2018 ZeroC, Inc. All rights reserved.
//
// This copy of Ice is licensed to you under the terms described in the
// ICE_LICENSE file included in this distribution.
//
// **********************************************************************

package test.Ice.invoke;

import test.Ice.invoke.Test.MyClassPrx;

public class Client extends test.Util.Application
{
    @Override
    public int run(String[] args)
    {
        MyClassPrx myClass = AllTests.allTests(this);

        //
        // Use reflection to load lambda.AllTests as that is only supported with Java >= 1.8
        //
        try
        {
            Class<?> cls = IceInternal.Util.findClass("test.Ice.invoke.lambda.AllTests", null);
            if(cls != null)
            {
                java.lang.reflect.Method allTests = cls.getDeclaredMethod("allTests",
                    new Class<?>[]{test.Util.Application.class});
                allTests.invoke(null, this);
            }
        }
        catch(java.lang.NoSuchMethodException ex)
        {
            throw new RuntimeException(ex);
        }
        catch(java.lang.IllegalAccessException ex)
        {
            throw new RuntimeException(ex);
        }
        catch(java.lang.reflect.InvocationTargetException ex)
        {
            throw new RuntimeException(ex);
        }

        myClass.shutdown();

        return 0;
    }

    @Override
    protected Ice.InitializationData getInitData(Ice.StringSeqHolder argsH)
    {
        Ice.InitializationData initData = super.getInitData(argsH);
        initData.properties.setProperty("Ice.Package.Test", "test.Ice.invoke");
        return initData;
    }

    public static void main(String[] args)
    {
        Client c = new Client();
        int status = c.main("Client", args);

        System.gc();
        System.exit(status);
    }
}
